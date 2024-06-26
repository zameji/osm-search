import json
import math
import time
from datetime import datetime, timedelta
from functools import wraps
from re import match

import firebase_admin
import psycopg
from api_types import (
    Bbox,
    Filter,
    GeoJSON,
    PostgresConfig,
    RequestParams,
    RetrievedData,
    RetrievedDataRaw,
    Timeout,
)
from firebase_admin import auth, credentials, firestore
from flask import Flask, Request, Response, jsonify, request
from flask_cors import CORS
from loguru import logger
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import ValidationError

# Firebase initialization
cred = credentials.Certificate("service_account.json")
firebase_app = firebase_admin.initialize_app(cred)
db = firestore.client()

# Flask initialization
app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

CORS(app)


def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None

        if "Authorization" in request.headers:
            token = request.headers["Authorization"].split(" ")[1]
        if not token:
            logger.info("Request with missing authentication token.")
            return {
                "message": "Authentication Token is missing!",
                "data": None,
                "error": "Unauthorized",
            }, 401
        try:
            idinfo = auth.verify_id_token(token)

            if idinfo is None:
                logger.warning(f"Invalid authentication token {token}")
                return {
                    "message": "Invalid Authentication token!",
                    "data": None,
                    "error": "Unauthorized",
                }, 403
        except Exception as e:
            logger.warning(f"Other error {e}")
            return {
                "message": "Something went wrong",
                "data": None,
                "error": str(e),
            }, 403

        logger.info(f"Authenticated request by {idinfo['email']}")
        return f(*args, **kwargs)

    return decorated


def get_user(request: Request) -> dict | None:
    token = None

    if "Authorization" in request.headers:
        token = request.headers["Authorization"].split(" ")[1]

    if not token:
        return None

    idinfo = auth.verify_id_token(token)
    return idinfo


def get_db_connection() -> psycopg.Connection:
    db_config = PostgresConfig()
    conn = psycopg.connect(
        f"dbname={db_config.database} user={db_config.user} password={db_config.password} host={db_config.host} port={db_config.port}"
    )

    return conn


def query_with_timing(
    query, conn: psycopg.Connection | None = None
) -> tuple[list[RetrievedDataRaw], timedelta]:
    if conn is None:
        conn = get_db_connection()

    cur: psycopg.Cursor = conn.cursor(row_factory=dict_row)

    cur.execute("SET SESSION statement_timeout = '100s';")

    t1: datetime = datetime.now()
    try:
        cur.execute(query)
    except psycopg.errors.QueryCanceled:
        logger.warning("Request timed out")
        raise Timeout()

    data: list[RetrievedDataRaw] = cur.fetchall()
    cur.close()
    conn.close()

    t2 = datetime.now()

    return (data, t2 - t1)


def format_results_log(data: list[RetrievedDataRaw]) -> list[dict]:
    LOGGED_KEYS = {"name", "lat", "lng", "point_geom"}
    return [{k: v for k, v in x.items() if k in LOGGED_KEYS} for x in data]


def format_filters_log(filters: list[Filter]) -> list[dict]:
    filters_dict = [dict(filter) for filter in filters]

    filters_formatted = []
    for f in filters_dict:
        subfilters = list(map(dict, f["filters"]))
        filters_formatted.append({**f, "filters": subfilters})
    return filters_formatted


def log_query(
    request: Request,
    query_string: str,
    bbox: Bbox,
    filters: list[Filter],
    tdiff: timedelta,
    data: list[RetrievedDataRaw],
):
    timestamp: int = time.time_ns()  # type: ignore
    user: dict | None = get_user(request)

    log_data = {
        "timestamp": firestore.SERVER_TIMESTAMP,
        "query": query_string,
        "user_uid": user["uid"] if user else None,
        "user_email": user["email"] if user else None,
        "bbox": list(bbox),
        "filters": format_filters_log(filters),
        "query_time": tdiff.total_seconds(),
        "query_nresults": len(data),
        "query_results": format_results_log(data),
    }

    db.collection("searches").document(str(timestamp)).set(log_data)


def get_source_table(f: Filter) -> sql.Composed:
    match f.type:
        case "point":
            return sql.SQL("planet_osm_point")
        case "line":
            return sql.SQL("planet_osm_line")
        case "polygon":
            return sql.SQL("planet_osm_polygon")
        case "any":
            return sql.SQL("planet_osm")


def build_filter_query(filter: Filter):
    subfilters_list: list[Filter] = []
    for subfilter in filter.filters:
        if subfilter.comparison == "=":
            filter_query = sql.SQL("(tags @> {match})").format(
                match=sql.Literal(subfilter.parameter + "=>" + subfilter.value),
            )
        elif subfilter.comparison == "!=":
            filter_query = sql.SQL("NOT(tags @> {match})").format(
                match=sql.Literal(subfilter.parameter + "=>" + subfilter.value),
            )
        elif subfilter.comparison == "is null":
            filter_query = sql.SQL("NOT(tags?{parameter})").format(
                parameter=sql.Literal(subfilter.parameter)
            )
        elif subfilter.comparison == "is not null":
            filter_query = sql.SQL("(tags?{parameter})").format(
                parameter=sql.Literal(subfilter.parameter)
            )
        else:
            parameter = sql.SQL("tags->{parameter}").format(
                parameter=sql.Literal(subfilter.parameter)
            )

            if subfilter.cast is not None:
                if subfilter.cast == "cast_to_float":
                    parameter = sql.SQL("cast_to_float({parameter}, 0.0)").format(
                        parameter=parameter
                    )
                elif subfilter.cast == "cast_to_int":
                    parameter = sql.SQL("cast_to_int({parameter}, 0)").format(
                        parameter=parameter
                    )
                else:
                    parameter = sql.SQL("CAST({parameter} AS {cast})").format(
                        parameter=parameter, cast=sql.SQL(subfilter.cast)
                    )

            if subfilter.comparison in ("starts with", "ends with", "contains"):
                comparison = "ILIKE"
            elif subfilter.comparison == "does not contain":
                comparison = "NOT ILIKE"
            else:
                comparison = subfilter.comparison

            match subfilter.comparison:
                case "starts with":
                    value = f"{subfilter.value}%"
                case "ends with":
                    value = f"%{subfilter.value}"
                case "contains" | "does not contain":
                    value = f"%{subfilter.value}%"
                case _:
                    value = subfilter.value

            filter_query = sql.SQL("({parameter} {comparison} {value})").format(
                parameter=parameter,
                comparison=sql.SQL(comparison),
                value=sql.Literal(value),
            )

        subfilters_list.append(filter_query)

    if filter.method == "AND":
        joiner = sql.SQL(" AND ")
    else:
        joiner = sql.SQL(" OR ")

    return joiner.join(subfilters_list)


def build_cte_from_filter(f: Filter, id: sql.SQL) -> sql.Composed:
    source_table = get_source_table(f)
    filter_query = build_filter_query(f)

    assembled: sql.Composed = sql.SQL(
        """{id} AS (SELECT
        way AS geom,
        osm_id
        FROM {source_table}, envelope
        WHERE ({filter}) AND (way && envelope.geom))"""
    ).format(id=id, source_table=source_table, filter=filter_query)
    return assembled


def generate_join_statements(
    cte_list: list[tuple[sql.Composed, sql.Composed]], buffer: int
) -> sql.Composed:
    return sql.SQL(" ").join(
        [
            sql.SQL(
                "JOIN {cte_id} ON ST_DWithin(initial_table.geom, {cte_id}.geom, {buffer}) AND {self_join_exclusion_clause}"
            ).format(
                cte_id=cte_id,
                buffer=sql.Literal(buffer),
                self_join_exclusion_clause=sql.SQL(" AND ").join(
                    [
                        sql.SQL("{cte_id}.osm_id != initial_table.osm_id").format(
                            cte_id=cte_id
                        )
                    ]
                    + [
                        sql.SQL("{cte_id}.osm_id != {cte_id2}.osm_id").format(
                            cte_id=cte_id, cte_id2=cte_id2
                        )
                        for cte_id2, _ in cte_list[0:index]
                        if cte_id2 != cte_id
                    ]
                ),
            )
            for index, [cte_id, cte_sql] in enumerate(cte_list)
        ]
    )


def build_query(
    filters: list[Filter], bbox: Bbox, buffer: int, limit: int, offset: int
) -> sql.Composed:
    logger.info(f"Buffer: {buffer}\tFilters: {filters}\tBbox: {str(bbox)}")

    envelope_cte: sql.Composed = sql.SQL(
        """WITH envelope AS (
            SELECT ST_Transform(ST_MakeEnvelope({left}, {bottom}, {right}, {top}, 4326), 3857) AS geom
        )"""
    ).format(
        left=sql.Literal(bbox.l),
        bottom=sql.Literal(bbox.b),
        right=sql.Literal(bbox.r),
        top=sql.Literal(bbox.t),
    )

    first: Filter = filters.pop(0)
    source_table = get_source_table(first)
    initial_cte = sql.SQL(
        """initial_table AS (
            SELECT
                osm_id,
                tags->'name' AS name,
                ST_Transform(ST_Centroid(way), 4326) AS point_geom,
                way AS geom
            FROM {main_table}, envelope
            WHERE ({filter}) AND (way && envelope.geom))"""
    ).format(main_table=source_table, filter=build_filter_query(first))

    additional_cte_list: list[tuple[sql.Composed, sql.Composed]] = []
    for i, f in enumerate(filters):
        cte_id = sql.SQL("subquery{index}").format(index=sql.SQL(str(i)))
        additional_cte_list.append((cte_id, build_cte_from_filter(f, cte_id)))

    query = sql.SQL(
        "{envelope_cte}, {ctes} {select_statement_initial} {select_statements_cte} {from_statement} {join_staments} {pagination}"
    ).format(
        envelope_cte=envelope_cte,
        ctes=sql.SQL(", ").join(
            [initial_cte, *[cte_sql for cte_id, cte_sql in additional_cte_list]]
        ),
        select_statement_initial=sql.SQL(
            "SELECT point_geom, name, ST_Y(point_geom) AS lat, ST_X(point_geom) AS lng, ST_AsGeoJSON(ST_Transform(initial_table.geom, 4326)) AS main_geom"
            + ("" if len(additional_cte_list) == 0 else ",")
        ),
        select_statements_cte=sql.SQL(" ,").join(
            [
                sql.SQL(
                    "ARRAY_AGG(DISTINCT ST_AsGeoJSON(ST_Transform({cte_id}.geom, 4326))) AS {cte_id}_geom"
                ).format(cte_id=cte_id)
                for cte_id, cte_sql in additional_cte_list
            ]
        ),
        from_statement=sql.SQL("FROM initial_table"),
        join_staments=generate_join_statements(additional_cte_list, buffer),
        pagination=sql.SQL(
            "GROUP BY point_geom, name, initial_table.geom ORDER BY point_geom, name, lat, lng LIMIT {limit} OFFSET {offset}"
        ).format(limit=sql.Literal(limit + 1), offset=sql.Literal(offset)),
    )

    return query


def format_retrieved_data(data: RetrievedDataRaw) -> RetrievedData:
    geometries: list[GeoJSON] = [json.loads(data["main_geom"])]
    geometry_columns = [
        column for column in data.keys() if match(r"^subquery\d+_geom$", column)
    ]
    for column in geometry_columns:
        geometries.extend([json.loads(item) for item in data[column]])  # type: ignore

    return {
        "name": data["name"],
        "lat": data["lat"],
        "lng": data["lng"],
        "geometry": geometries,
    }


@app.route("/intersection")
@token_required
def get_intersection() -> tuple[Response, int] | Response:
    try:
        params = RequestParams(
            buffer=request.args.get("buffer"),
            filters=request.args.get("filters"),
            l=request.args.get("l"),
            b=request.args.get("b"),
            r=request.args.get("r"),
            t=request.args.get("t"),
            limit=request.args.get("limit"),
            page=request.args.get("page"),
        )
    except ValidationError as e:
        return jsonify({"error": "Invalid data", "details": e.errors()}), 400

    buffer: int = params.buffer
    filters: list[Filter] = params.filters

    bbox: Bbox = Bbox(params.l, params.b, params.r, params.t)
    limit = min(params.limit, 5)
    offset = params.page * limit

    area: float = (
        math.pow(6371, 2)
        * math.pi
        * abs(math.sin(math.radians(bbox.t)) - math.sin(math.radians(bbox.b)))
        * abs(bbox.r - bbox.l)
        / 180
    )

    # reject queries that are too large
    if area > 4e6:
        return Response(status=400)

    query = build_query(filters, bbox, buffer, limit, offset)
    conn: psycopg.Connection = get_db_connection()

    query_string: str = query.as_string(conn)
    logger.info(f"Executing query: {query_string}")

    try:
        data, tdiff = query_with_timing(query, conn=conn)
    except Timeout:
        return Response(status=408)

    data_formatted = list(map(format_retrieved_data, data))
    log_query(request, query_string, bbox, filters, tdiff, data)

    logger.info(f"Found {len(data)} results in {tdiff} seconds")

    if len(data) == 0:
        return jsonify({"message": "No results found", "data": []})

    # We fetch one extra result to determine if there are more results
    has_more = len(data_formatted) > limit

    return jsonify(
        {
            "data": data_formatted[0 : min(len(data_formatted), limit)],
            "has_more": has_more,
        }
    )


@app.route("/robots.txt")
def robots() -> Response:
    return Response("User-agent: *\nDisallow: /", mimetype="text/plain")


def start():
    app.run(port=5050)


if __name__ == "__main__":
    start()
