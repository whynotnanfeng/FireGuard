<template>
  <a-config-provider :locale="enUS">
    <router-view />
  </a-config-provider>
</template>

<script setup lang="ts">
import enUS from 'ant-design-vue/es/locale/en_US';
import dayjs from 'dayjs';
import 'dayjs/locale/en';
dayjs.locale('en');
</script>
