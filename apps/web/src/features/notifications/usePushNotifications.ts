/** Состояние Web Push на странице: проверка при загрузке и действия человека. */
import { useCallback, useEffect, useRef, useState } from "react"

import { detectState, disable, enable, type PushSubscriptionApi, type PushEnv, type PushState } from "./pushClient"

export interface UsePushNotifications {
  readonly state: PushState
  turnOn(): void
  turnOff(): void
}

export function usePushNotifications(env: PushEnv | null, api: PushSubscriptionApi | undefined): UsePushNotifications {
  const [state, setState] = useState<PushState>(
    env === null || api === undefined ? { kind: "unsupported" } : { kind: "checking" },
  )
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    if (env === null || api === undefined) return
    void detectState(env, api).then((next) => {
      if (mounted.current) setState(next)
    })
    return () => {
      mounted.current = false
    }
  }, [env, api])

  const turnOn = useCallback(() => {
    if (env === null || api === undefined) return
    setState({ kind: "working" })
    void enable(env, api).then((next) => {
      if (mounted.current) setState(next)
    })
  }, [env, api])

  const turnOff = useCallback(() => {
    if (env === null || api === undefined) return
    setState({ kind: "working" })
    void disable(env, api).then((next) => {
      if (mounted.current) setState(next)
    })
  }, [env, api])

  return { state, turnOn, turnOff }
}
