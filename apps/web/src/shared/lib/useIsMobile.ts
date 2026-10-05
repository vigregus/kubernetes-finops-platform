import { useSyncExternalStore } from "react"

/**
 * Ширина, с которой раскладка становится одноколоночной. Совпадает с `md` у Tailwind
 * (768 px): ниже — телефон и узкое окно, где список бесед и беседа занимают экран по очереди.
 */
export const MOBILE_QUERY = "(max-width: 767px)"

function subscribe(onChange: () => void): () => void {
  const query = window.matchMedia?.(MOBILE_QUERY)
  query?.addEventListener("change", onChange)
  return () => query?.removeEventListener("change", onChange)
}

function snapshot(): boolean {
  return window.matchMedia?.(MOBILE_QUERY).matches ?? false
}

/**
 * Узкий экран ли сейчас. Решает **JS**, а не только CSS: на узком экране панель
 * беседы не должна быть смонтирована, пока человек её не открыл. Спрятанная
 * стилем, она держала бы соединение и отправляла бы квитанции о прочтении беседы,
 * которую никто не открывал. Среда без `matchMedia` (тесты) — не телефон.
 */
export function useIsMobile(): boolean {
  return useSyncExternalStore(subscribe, snapshot, () => false)
}
