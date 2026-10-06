import type { BrowserContext, Page } from "@playwright/test"

/**
 * Общее для браузерных проверок звонков: приёмка G4 (`g4-calls.spec.ts`) и
 * нагрузочный canary (`canary-calls.spec.ts`).
 */

/**
 * Записывает все соединения страницы, чтобы потом спросить у них статистику.
 * Продакшен-код не отдаёт соединение наружу (и не должен), поэтому приёмка
 * оборачивает конструктор до загрузки страницы.
 */
export async function trackPeers(context: BrowserContext): Promise<void> {
	await context.addInitScript(() => {
		const Original = window.RTCPeerConnection
		const peers: RTCPeerConnection[] = []
		;(window as unknown as { __peers: RTCPeerConnection[] }).__peers = peers
		window.RTCPeerConnection = new Proxy(Original, {
			construct(target, args) {
				const peer = new target(...(args as [RTCConfiguration?]))
				peers.push(peer)
				return peer
			},
		})
	})
}

/** Сколько пакетов принято по входящим потокам; растёт — значит, медиа идёт. */
export async function packetsReceived(page: Page): Promise<number> {
	return page.evaluate(async () => {
		const peers = (window as unknown as { __peers?: RTCPeerConnection[] }).__peers ?? []
		let total = 0
		for (const peer of peers) {
			const report = await peer.getStats()
			report.forEach((entry) => {
				if (entry.type === "inbound-rtp") total += entry.packetsReceived ?? 0
			})
		}
		return total
	})
}
