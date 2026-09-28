/**
 * Звук нового сообщения — две короткие ноты через Web Audio, без файла.
 *
 * Бинарник в дереве завёл бы источник правды рядом с кодом: звук нельзя было
 * бы прочесть в ревью, а лицензия файла — вопрос, которого у сгенерированного
 * тона просто нет. `AudioContext` держится один на вкладку и создаётся лениво,
 * при первом звуке, а не при импорте модуля: часть браузеров создаёт его в
 * состоянии `suspended` до первого жеста человека, и ранний вызов
 * `new AudioContext()` вне обработчика клика был бы избыточным риском без
 * выгоды — момент первого сообщения всё равно наступит позже композитора.
 *
 * Отказ проигрывания — не сбой соединения и не предмет этого модуля: вкладка
 * в фоне, автовоспроизведение ещё не разрешено политикой браузера, звук
 * пользователем не запрошен вовсе — три разных причины с одним и тем же
 * ответом. Молчание здесь честнее, чем брошенное исключение, унёсшее
 * обработчик публикации.
 */
let audioContext: AudioContext | null = null

function contextOf(): AudioContext | null {
  if (audioContext !== null) return audioContext
  const Ctor = window.AudioContext ?? (window as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (Ctor === undefined) return null
  audioContext = new Ctor()
  return audioContext
}

/** Одна нота: синусоида с коротким затуханием, чтобы не звучать щелчком. */
function tone(context: AudioContext, frequencyHz: number, startAt: number, durationSeconds: number): void {
  const oscillator = context.createOscillator()
  const gain = context.createGain()
  oscillator.frequency.value = frequencyHz
  oscillator.type = "sine"
  gain.gain.setValueAtTime(0.0001, startAt)
  gain.gain.exponentialRampToValueAtTime(0.2, startAt + 0.01)
  gain.gain.exponentialRampToValueAtTime(0.0001, startAt + durationSeconds)
  oscillator.connect(gain)
  gain.connect(context.destination)
  oscillator.start(startAt)
  oscillator.stop(startAt + durationSeconds)
}

/** Зовётся на каждое входящее `message.created` от собеседника, не от себя. */
export function playIncomingMessageSound(): void {
  try {
    const context = contextOf()
    if (context === null) return
    if (context.state === "suspended") void context.resume()
    const now = context.currentTime
    tone(context, 880, now, 0.12)
    tone(context, 1318.5, now + 0.09, 0.15)
  } catch {
    // См. докстринг модуля: отказ звука не предмет этого пути.
  }
}
