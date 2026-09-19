import { LoginScreen } from "../design-system/organisms/LoginScreen"

interface LoginPageProps {
  sessionExpired?: boolean
}

export function LoginPage({ sessionExpired }: LoginPageProps) {
  return <LoginScreen sessionExpired={sessionExpired} onLogin={() => {}} />
}
