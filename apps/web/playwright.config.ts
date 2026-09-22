import { defineConfig } from "@playwright/test"

/**
 * Конфиг браузерной приёмки G3-005.
 *
 * `testDir` смотрит за пределы `apps/web`, и это не небрежность: спека живёт
 * в `tests/e2e/` рядом с остальными проверками. Раннер при этом берётся из
 * `apps/web/node_modules` — ради этого рядом лежит `tests/e2e/tsconfig.json`
 * с картой путей (в репозитории нет корневого `node_modules`, и без карты
 * спека не находит `@playwright/test` вовсе).
 *
 * Адреса стенда сюда не вписаны: их задаёт `scripts/web-e2e-fixture.sh`
 * переменными окружения. Приёмка без оркестратора не запускается — и падает
 * внятно, а не «Invalid URL»: `E2E_*` читаются проверяемо в самих тестах.
 */
export default defineConfig({
	testDir: "../../tests/e2e",
	// Одна вкладка за раз: приёмка ходит на один стенд одними и теми же
	// учётными записями, и второй одновременный вход с той же записью снял бы
	// сессию первого.
	workers: 1,
	fullyParallel: false,
	forbidOnly: Boolean(process.env.CI),
	retries: 0,
	reporter: [["list"]],
	// Артефакты — внутрь `node_modules`: каталог по умолчанию (`test-results/`)
	// оказался бы в рабочем дереве репозитория.
	outputDir: "node_modules/.cache/playwright-artifacts",
	timeout: 120_000,
	expect: { timeout: 20_000 },
	use: {
		baseURL: process.env.E2E_BASE_URL,
		// Локальный удостоверяющий центр стенда: сертификаты выписаны им, и
		// браузеру в приёмке он не доверяет. Имена при этом настоящие —
		// подмены адреса нет, только доверие к сертификату.
		ignoreHTTPSErrors: true,
		trace: "retain-on-failure",
	},
	projects: [
		// Подготовка и приёмка — разные работы: первая заводит фикстуры и беседу
		// и пишет состояние в файл, вторая читает это состояние. Проект
		// `acceptance` зависит от `fixture`, поэтому порядок задан раннером, а не
		// порядком строк в спеке.
		//
		// `fixture.setup.ts` не импортируется спекой намеренно: модуль с `test()`
		// верхнего уровня был бы собран в проект импортирующего файла, то есть
		// подготовка побежала бы ещё раз, внутри самой приёмки.
		{ name: "fixture", testMatch: /fixture\.setup\.ts$/ },
		{
			name: "acceptance",
			testMatch: /g3-005-web\.spec\.ts$/,
			dependencies: ["fixture"],
		},
	],
})
