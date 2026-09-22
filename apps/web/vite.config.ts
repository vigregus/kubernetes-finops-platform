/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig, type Plugin, type PreviewServer, type ViteDevServer } from 'vite';

// https://vite.dev/config/
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { storybookTest } from '@storybook/addon-vitest/vitest-plugin';
import { playwright } from '@vitest/browser-playwright';
const dirname = typeof __dirname !== 'undefined' ? __dirname : path.dirname(fileURLToPath(import.meta.url));

/**
 * `/runtime/runtime-config.js` в разработке и предпросмотре.
 *
 * В образе этого файла нет и быть не должно: он приходит томом из ConfigMap
 * окружения, а вшитый в статику он стал бы значением по умолчанию, которое том
 * лишь прячет. Но `vite dev` и `vite preview` статику не собирают — они отдают
 * её из памяти и из каталога, — и без этого ответа вход падал бы на
 * отсутствующей конфигурации, то есть на требовании, адресованном
 * развёртыванию, а не разработчику.
 *
 * Отвечает по тому же пути и с тем же именем переменной, что и стенд, поэтому
 * расхождения между «работает локально» и «работает в кластере» здесь не
 * заводится: значение берётся из окружения, а не из кода. `public/` для этого
 * не годится — его содержимое уезжает в сборку, а файла там быть не должно.
 */
function runtimeConfigForDev(): Plugin {
  const issuer = process.env.MESSENGER_OIDC_ISSUER ?? 'https://idp.finops.local/realms/messenger';
  // Второе поле появилось вместе с соединением (G3-006) и здесь обязано быть
  // по той же причине, что и первое: `loadRuntimeConfig` требует оба поля, и
  // без этой строки `vite dev` падал бы на конфигурации, которой не хватает
  // адреса. Имя публичное — то же, что у стенда: кластерное `loadRuntimeConfig`
  // отвергает, и локальная разработка падала бы на нём же.
  const centrifugo =
    process.env.MESSENGER_CENTRIFUGO_URL ?? 'wss://rt.finops.local/connection/websocket';
  const body =
    `window.__MESSENGER_RUNTIME_CONFIG__ = Object.freeze(` +
    `${JSON.stringify({ oidcIssuer: issuer, centrifugoUrl: centrifugo })});\n`;

  // Объединение, а не `ViteDevServer`: предпросмотр отдаёт `PreviewServer`, и
  // подпись «сервер разработки» была бы неверна ровно на том случае, ради
  // которого здесь стоит второй хук — `vite preview` по собранному `dist/`.
  // Используется у обоих одно и то же поле `middlewares`, и это не совпадение
  // типов, а всё, что плагину нужно.
  const serve = (server: ViteDevServer | PreviewServer): void => {
    server.middlewares.use('/runtime/runtime-config.js', (_req, res) => {
      res.setHeader('Content-Type', 'application/javascript; charset=utf-8');
      res.setHeader('Cache-Control', 'no-store');
      res.end(body);
    });
  };

  return {
    name: 'messenger:runtime-config',
    // Хуки сервера при `vite build` не вызываются вовсе, поэтому отдельного
    // `apply` не нужно: сборку этот плагин не трогает и в неё ничего не несёт.
    configureServer: serve,
    configurePreviewServer: serve,
  };
}

// More info at: https://storybook.js.org/docs/next/writing-tests/integrations/vitest-addon
export default defineConfig({
  plugins: [react(), tailwindcss(), runtimeConfigForDev()],
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