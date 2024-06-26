<template>
  <v-row class="mb-8 ml-5">
    <v-btn @click="isDialogOpen = true" color="secondary" size="small" rounded>
      <v-icon>mdi-plus</v-icon>
      <span>Custom feature</span>
    </v-btn>
  </v-row>
  <v-dialog v-model="isDialogOpen">
    <v-card variant="flat">
      <v-card-title>
        <v-row class="ma-0">
          Custom feature
          <v-spacer></v-spacer>
          <v-btn @click="isDialogOpen = false" icon="mdi-close" variant="flat">
          </v-btn>
        </v-row>
      </v-card-title>
      <v-card-text>
        <v-row v-for="(f, i) in filters" :key="'row' + i">
          <v-col cols="2"
            ><v-select
              v-if="i == 0"
              label="Feature type"
              :items="queryTypes"
              v-model="selectedQueryType"
            ></v-select>
            <v-select
              v-else-if="i == 1"
              label="Condition"
              :items="['OR', 'AND']"
              v-model="method"
            ></v-select> </v-col
          ><v-col>
            <!-- Text field for OSM parameter -->
            <v-combobox
              class="code"
              label="OSM key"
              v-model="filters[i].parameter"
              :items="store.osmKeys"
              @update:modelValue="getValues"
            >
              <template
                v-slot:append
                v-if="
                  filters[i].parameter != '' && filters[i].parameter != null
                "
              >
                <a
                  :href="
                    'https://wiki.openstreetmap.org/wiki/Key:' +
                    filters[i].parameter
                  "
                  target="_blank"
                  class="super"
                >
                  <v-icon x-small>mdi-open-in-new</v-icon></a
                >
              </template>
            </v-combobox></v-col
          >
          <v-col>
            <!-- Dropdown for type of comparison between parameter and value -->
            <v-select
              label=""
              :items="[
                '=',
                '!=',
                '>',
                '<',
                '>=',
                '<=',
                'starts with',
                'ends with',
                'contains',
                'does not contain',
                'is null',
                'is not null',
              ]"
              v-model="filters[i].comparison"
            ></v-select>
          </v-col>
          <v-col>
            <!-- Text field for parameter value -->
            <v-combobox
              class="code"
              label="OSM value"
              v-model="filters[i].value"
              :items="store.selectedKeyValues[filters[i].parameter]"
              :disabled="
                filters[i].comparison == 'is null' ||
                filters[i].comparison == 'is not null'
              "
            ></v-combobox>
          </v-col>
          <v-col cols="1">
            <v-btn
              v-if="i > 0"
              @click="removeFilter(i)"
              icon="mdi-close"
              variant="text"
            >
            </v-btn>
          </v-col>
        </v-row>
        <v-row class="ma-0 justify-center">
          <v-btn rounded color="secondary" text @click="addFilter"
            >Add condition</v-btn
          >
        </v-row>
      </v-card-text>
      <v-card-actions>
        <v-spacer></v-spacer>
        <v-btn
          style="margin-left: auto"
          color="primary"
          text
          @click="addCustom"
          :disabled="filters[0].parameter == ''"
          >Add custom feature</v-btn
        >
      </v-card-actions>
    </v-card>
  </v-dialog>
</template>

<script setup lang="ts">
import { useAppStore } from "@/stores/app";
import { ref } from "vue";

const isDialogOpen = ref(false);
const store = useAppStore();
const queryTypes = ["any", "point", "line", "polygon"];
const selectedQueryType = ref("any");
const method = ref("OR");
const filters = ref([
  {
    parameter: "",
    comparison: "=",
    value: "",
  },
]);

function addCustom() {
  store.updateSelected([
    ...store.selected,
    {
      name: "Custom filter",
      type: selectedQueryType.value,
      filters: filters.value.filter((v) => v.parameter != ""),
      method: method.value,
      unsavedCustomFeature: true,
    },
  ]);

  method.value = "OR";
  filters.value = [
    {
      parameter: "",
      comparison: "=",
      value: "",
    },
  ];
  selectedQueryType.value = "any";
  isDialogOpen.value = false;
}

function addFilter() {
  filters.value.push({
    parameter: "",
    comparison: "=",
    value: "",
  });
}

function removeFilter(index) {
  filters.value = [
    ...filters.value.slice(0, index),
    ...filters.value.slice(index + 1),
  ];
}

function getValues(v) {
  store.getValues(v);
}
</script>
