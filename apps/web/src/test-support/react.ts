import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

/**
 * Размонтирование между тестами.
 *
 * `@testing-library/react` навешивает `afterEach(cleanup)` сам **только** при
 * включённых глобалях, а unit-проект их не включает (`globals` не задан). Без
 * этой строки разметка копится от теста к тесту, и второй `render` в файле
 * падает на «Found multiple elements» — падение, которое выглядит дефектом
 * компонента и им не является. Отдельный `import { cleanup }` в каждом файле
 * решал бы то же самое, но повторялся бы ровно там, где о нём забудут.
 */
afterEach(cleanup);
