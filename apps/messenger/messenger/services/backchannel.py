"""OIDC Back-Channel Logout: внешний отзыв становится локальным немедленно."""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from messenger.adapters import oidc
from messenger.adapters.centrifugo import CentrifugoClient
from messenger.domain.identity import TokenRejection
from messenger.domain.session import RevocationReason, session_id_from_external
from messenger.repositories import sessions, users
from messenger.services import session_management
from messenger.telemetry import metrics


@dataclass(frozen=True, slots=True)
class BackchannelResult:
    accepted: bool = False
    revoked: int = 0
    rejection: TokenRejection | None = None


async def handle_logout(
    conn: asyncpg.Connection,
    *,
    token: str,
    keys: oidc.JwksCache,
    settings: oidc.OidcSettings,
    audience: str,
    realtime: CentrifugoClient | None,
) -> BackchannelResult:
    check = await oidc.verify_logout_token(
        token, keys=keys, settings=settings, audience=audience
    )
    if not check.ok or check.claims is None:
        return BackchannelResult(rejection=check.rejection or TokenRejection.MALFORMED)

    claims = check.claims
    if claims.session_state:
        session = await sessions.fetch_session(
            conn, session_id=session_id_from_external(claims.session_state)
        )
        if session is None:
            return BackchannelResult(accepted=True)
        revoked = await sessions.revoke_session(
            conn,
            session_id=session.session_id,
            user_id=session.user_id,
            reason=RevocationReason.PASSWORD_CHANGE,
        )
        if revoked is None:
            return BackchannelResult(accepted=True)
        metrics.sessions_revoked(RevocationReason.PASSWORD_CHANGE.value)
        await session_management.drop_connections(
            realtime,
            user_id=str(session.user_id),
            revoked=[revoked],
            disconnect_by_user=False,
        )
        return BackchannelResult(accepted=True, revoked=1)

    user = await users.fetch_user_by_external_id(conn, external_id=claims.subject)
    if user is None:
        return BackchannelResult(accepted=True)
    revoked = await sessions.revoke_user_sessions(
        conn, user_id=user.user_id, reason=RevocationReason.PASSWORD_CHANGE
    )
    metrics.sessions_revoked(RevocationReason.PASSWORD_CHANGE.value, len(revoked))
    if revoked:
        await session_management.drop_connections(
            realtime,
            user_id=str(user.user_id),
            revoked=revoked,
            disconnect_by_user=True,
        )
    return BackchannelResult(accepted=True, revoked=len(revoked))
