/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vite';

// https://vite.dev/config/
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { storybookTest } from '@storybook/addon-vitest/vitest-plugin';
import { playwright } from '@vitest/browser-playwright';
const dirname = typeof __dirname !== 'undefined' ? __dirname : path.dirname(fileURLToPath(import.meta.url));

// More info at: https://storybook.js.org/docs/next/writing-tests/integrations/vitest-addon
export default defineConfig({
  plugins: [react(), tailwindcss()],
  test: {
    projects: [{
      // Явный unit-проект, и он здесь не для красоты.
      //
      // Конфигурация, объявляющая `projects`, сама тесты не запускает, а
      // единственный проект до этой правки — `storybook` (браузерный, через
      // `@storybook/addon-vitest`) — собирает истории, а не `src/**/*.test.ts`.
      // То есть `client.test.ts`, `contract.test.ts` и все последующие
      // детекторы гейта в прогон не попадали бы вовсе, и «красный прогон»
      // был бы красным по пустоте: прогон, не собравший ни одного теста,
      // неотличим от прогона, где дефекта нет.
      extends: true,
      test: {
        name: 'unit',
        // `.tsx` здесь не для полноты: `component`-строки раздела F — это
        // детекторы на элемент в дереве (нет «Active now», нет кнопки без
        // обработчика), а они пишутся разметкой. Шаблон без `.tsx` оставлял бы
        // такой файл несобранным, и зелёный прогон не отличался бы от «файла
        // нет вовсе»: число собранных тестов не выросло бы ни на единицу.
        include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
        // Размонтирование React между тестами — здесь, а не в каждом файле:
        // без глобального `afterEach` библиотека рендера не убирает дерево сама.
        setupFiles: ['src/test-support/react.ts'],
        environment: 'jsdom'
      }
    }, {
      extends: true,
      plugins: [
      // The plugin will run tests for the stories defined in your Storybook config
      // See options at: https://storybook.js.org/docs/next/writing-tests/integrations/vitest-addon#storybooktest
      storybookTest({
        configDir: path.join(dirname, '.storybook')
      })],
      test: {
        name: 'storybook',
        browser: {
          enabled: true,
          headless: true,
          provider: playwright({}),
          instances: [{
            browser: 'chromium'
          }]
        }
      }
    }]
  }
});