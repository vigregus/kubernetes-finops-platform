/**
 * Двойник `IntersectionObserver` — общий для ленты и для панели.
 *
 * Наблюдателя в jsdom нет вовсе (`D13` называет это ценой, а не бедой
 * окружения), поэтому «прочитано» проверяется подставленным наблюдателем, а не
 * его отсутствием. Двойник лежит в `test-support/`, а не рядом с тестом, по той
 * же причине, что и двойник Centrifugo: он нужен **двоим** — `MessageList`
 * заводит наблюдателя сам, а `ChatPage` — через него, и оба обязаны видеть одну
 * и ту же его поверхность. Второй экземпляр разошёлся бы с первым молча, и
 * расхождение выглядело бы как «панель не сообщает о видимых строках».
 *
 * Что двойник **воспроизводит**, и почему именно это:
 *
 * * **опции** — `root` и `threshold`. Компонент просит браузер о полном
 *   пересечении (`threshold: 1.0`) и о прокрутке ленты вместо окна (`root`), и
 *   проверка обязана читать то, что он попросил, а не то, что удобно двойнику;
 * * **отношение пересечения** — `intersectionRatio`, которым браузер отвечает.
 *   Опцию `threshold` двойник исполнить не может (он не считает геометрию), и
 *   это намеренно: условие `intersectionRatio >= 1` живёт в компоненте, и тест,
 *   называющий долю пересечения сам, проверяет именно его. Двойник, «исполняющий»
 *   порог, превратил бы проверку в тавтологию;
 * * **цели** — множество наблюдемых элементов в том виде, в каком его набрал
 *   компонент: по нему видно, что наблюдают именно строки, и именно те.
 *
 * Чего он **не** воспроизводит — асинхронность настоящего наблюдателя: колбэк
 * зовётся внутри `act`, синхронно с вызовом теста. Ждать здесь нечего: уведомление
 * браузера — вход, а не результат рендера.
 */

import { act } from "@testing-library/react";

/** Двойник наблюдателя: запоминает опции и отдаёт колбэк наружу. */
export class FakeIntersectionObserver {
  /** Все заведённые экземпляры — по одному на каждый коллектор компонента. */
  static instances: FakeIntersectionObserver[] = [];

  readonly targets = new Set<Element>();

  /**
   * Опции и колбэк — полями, а не параметрами конструктора.
   *
   * `readonly callback` в списке параметров сделал бы то же самое в одну строку,
   * но это `erasableSyntaxOnly`: параметр-свойство компилируется в присваивание
   * в теле конструктора, то есть в код, которого в исходнике нет, — а проект
   * требует, чтобы каждый файл читался как есть.
   */
  readonly callback: IntersectionObserverCallback;

  readonly options: IntersectionObserverInit | undefined;

  constructor(
    callback: IntersectionObserverCallback,
    options: IntersectionObserverInit | undefined,
  ) {
    this.callback = callback;
    this.options = options;
    FakeIntersectionObserver.instances.push(this);
  }

  /**
   * Последний заведённый наблюдатель — или падение.
   *
   * Падение, а не `undefined`, намеренно: тест, не получивший наблюдателя,
   * проверял бы пустоту и зеленел бы на том самом дефекте, ради которого
   * написан («колбэк не прокинут»). Ошибка здесь называет дефект, а не
   * заставляет искать его в `expect`.
   */
  static get latest(): FakeIntersectionObserver {
    const observer = FakeIntersectionObserver.instances.at(-1);
    if (observer === undefined) throw new Error("наблюдатель не заведён");

    return observer;
  }

  observe(target: Element): void {
    this.targets.add(target);
  }

  unobserve(target: Element): void {
    this.targets.delete(target);
  }

  disconnect(): void {
    this.targets.clear();
  }

  /** Пересечение элемента с окном — так, как его описал бы браузер. */
  intersect(target: Element, ratio: number): void {
    const entry = {
      target,
      isIntersecting: ratio > 0,
      intersectionRatio: ratio,
    } as unknown as IntersectionObserverEntry;

    act(() => {
      this.callback([entry], this as unknown as IntersectionObserver);
    });
  }

  /**
   * Пересечение строки ленты — по **производственному** атрибуту.
   *
   * `data-message-seq` — то, чем компонент адресует строки в колбэке, и то, по
   * чему номер читает приёмка. Тест, ищущий строку по тестовому идентификатору,
   * проверял бы шов, которого в проде нет.
   */
  intersectSeq(seq: number, ratio: number): void {
    const target = [...this.targets].find(
      (element) => element.getAttribute("data-message-seq") === String(seq),
    );
    if (target === undefined) throw new Error(`строка ${seq} не наблюдается`);

    this.intersect(target, ratio);
  }
}

const originalObserver = globalThis.IntersectionObserver;

/**
 * Ставит двойника и обнуляет список экземпляров.
 *
 * Обнуление здесь, а не в `beforeEach` каждого файла: `latest` иначе вернул бы
 * наблюдателя **прошлого** теста, и проверка опций читала бы чужой рендер.
 */
export function installObserverForJsdom(): void {
  FakeIntersectionObserver.instances = [];
  (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver =
    FakeIntersectionObserver;
}

/**
 * Возвращает `IntersectionObserver` в исходное состояние.
 *
 * Именно в **исходное**, а не в `undefined`: в jsdom его нет, и удаление тут —
 * восстановление, а не уборка. Но если прогон когда-нибудь пойдёт под браузером
 * (проект `storybook` — браузерный), настоящий наблюдатель обязан вернуться, а
 * не остаться подменённым для следующих файлов.
 */
export function restoreIntersectionObserver(): void {
  if (originalObserver === undefined) {
    delete (globalThis as { IntersectionObserver?: unknown }).IntersectionObserver;
  } else {
    globalThis.IntersectionObserver = originalObserver;
  }
}
