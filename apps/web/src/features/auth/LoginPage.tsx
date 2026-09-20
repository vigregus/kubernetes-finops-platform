import { LoginScreen } from "./components/LoginScreen"

interface LoginPageProps {
  sessionExpired?: boolean
}

export function LoginPage({ sessionExpired }: LoginPageProps) {
  return <LoginScreen sessionExpired={sessionExpired} onLogin={() => {}} />
}
