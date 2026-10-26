"""Transactional feedback tickets and event history; no network calls in transactions."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from .db import DbConn, transaction
from .i18n import UserFacingError

FINAL = frozenset({"resolved", "closed"})


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Ticket:
    number: int
    ticket_id: str
    author_id: str
    author_name: str
    assignee_id: str | None
    assignee_name: str | None
    status: str
    version: int
    created_at: str
    updated_at: str

    @property
    def ref(self) -> str:
        return f"FB-{self.number}"


@dataclass(frozen=True)
class Event:
    event_id: str
    ticket_number: int
    version: int
    kind: str
    actor_id: str
    actor_name: str
    body: str
    status: str
    assignee_id: str | None
    source_post_id: str | None
    created_at: str


class FeedbackRepo:
    def __init__(self, conn: DbConn) -> None:
        self.conn = conn

    def get(self, number: int) -> Ticket | None:
        row = self.conn.execute(
            "SELECT * FROM feedback_ticket WHERE number = ?", (number,)
        ).fetchone()
        return Ticket(**dict(row)) if row else None

    def previous(self, post_id: str, actor: str) -> Ticket | None:
        if not post_id:
            return None
        row = self.conn.execute(
            "SELECT ticket_number FROM feedback_event WHERE source_post_id = ? AND actor_id = ?",
            (post_id, actor),
        ).fetchone()
        return self.get(row["ticket_number"]) if row else None

    def visible(self, number: int, actor: str, handlers: frozenset[str]) -> Ticket:
        ticket = self.get(number)
        if ticket is None or (ticket.author_id != actor and actor not in handlers):
            raise UserFacingError("feedback.not_found")
        return ticket

    def listing(self, author: str | None, offset: int) -> list[Ticket]:
        if author is None:
            sql = (
                "SELECT * FROM feedback_ticket WHERE status NOT IN ('resolved', 'closed') "
                "ORDER BY CASE WHEN assignee_id IS NULL THEN 0 ELSE 1 END, number"
            )
            params: tuple[Any, ...] = ()
        else:
            sql = "SELECT * FROM feedback_ticket WHERE author_id = ? ORDER BY number DESC"
            params = (author,)
        rows = self.conn.execute(sql + " LIMIT 11 OFFSET ?", (*params, offset)).fetchall()
        return [Ticket(**dict(row)) for row in rows]

    def history(self, number: int, offset: int, limit: int = 2) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM feedback_event WHERE ticket_number = ? "
            "ORDER BY version LIMIT ? OFFSET ?",
            (number, limit, offset),
        ).fetchall()
        return [Event(**dict(row)) for row in rows]

    def latest(self, number: int) -> Event:
        row = self.conn.execute(
            "SELECT * FROM feedback_event WHERE ticket_number = ? ORDER BY version DESC LIMIT 1",
            (number,),
        ).fetchone()
        return Event(**dict(row))

    def _record(
        self,
        ticket: Ticket,
        kind: str,
        actor: str,
        body: str,
        post_id: str,
        handlers: frozenset[str],
        actor_name: str,
    ) -> None:
        event_id = uuid4().hex
        self.conn.execute(
            "INSERT INTO feedback_event (event_id, ticket_number, version, kind, actor_id, "
            "actor_name, body, status, assignee_id, source_post_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                ticket.number,
                ticket.version,
                kind,
                actor,
                actor_name,
                body,
                ticket.status,
                ticket.assignee_id,
                post_id or None,
                ticket.updated_at,
            ),
        )
        recipients = {ticket.author_id}
        if kind in {"new", "reopen"} or ticket.assignee_id not in handlers:
            recipients.update(handlers)
        elif ticket.assignee_id:
            recipients.add(ticket.assignee_id)
        for recipient in sorted(recipients - {actor}):
            self.conn.execute(
                "INSERT INTO feedback_outbox (outbox_id, event_id, recipient_id, next_attempt_at) "
                "VALUES (?, ?, ?, ?)",
                (uuid4().hex, event_id, recipient, ticket.updated_at),
            )

    def create(
        self,
        actor: str,
        body: str,
        post_id: str,
        handlers: frozenset[str],
        *,
        actor_name: str = "",
    ) -> Ticket:
        with transaction(self.conn):
            if previous := self.previous(post_id, actor):
                return previous
            stamp = now()
            row = self.conn.execute(
                "INSERT INTO feedback_ticket (ticket_id, author_id, author_name, status, version, "
                "created_at, updated_at) VALUES (?, ?, ?, 'new', 1, ?, ?) RETURNING *",
                (uuid4().hex, actor, actor_name or actor, stamp, stamp),
            ).fetchone()
            ticket = Ticket(**dict(row))
            self._record(ticket, "new", actor, body, post_id, handlers, actor_name or actor)
            return ticket

    def change(
        self,
        ticket: Ticket,
        kind: str,
        actor: str,
        body: str,
        post_id: str,
        handlers: frozenset[str],
        *,
        status: str | None = None,
        actor_name: str = "",
    ) -> Ticket:
        with transaction(self.conn):
            if previous := self.previous(post_id, actor):
                return previous
            # Recheck access and use a compare-and-swap below for concurrent writers.
            self.visible(ticket.number, actor, handlers)
            is_handler = actor in handlers
            state, assignee = ticket.status, ticket.assignee_id
            assignee_name = ticket.assignee_name
            if kind == "take":
                if not is_handler:
                    raise UserFacingError("feedback.forbidden")
                if state in FINAL or assignee in handlers:
                    raise UserFacingError("feedback.conflict")
                state, assignee = "in_progress", actor
                assignee_name = actor_name or actor
            elif kind == "reply":
                if state in FINAL:
                    raise UserFacingError("feedback.use_reopen", ref=ticket.ref)
                if actor == ticket.author_id and state == "waiting":
                    state = "in_progress"
            elif kind == "set-status":
                if not is_handler:
                    raise UserFacingError("feedback.forbidden")
                if assignee != actor or state in FINAL or status not in {"waiting", "resolved"}:
                    raise UserFacingError("feedback.conflict")
                state = status
            elif kind == "close":
                if state == "closed":
                    raise UserFacingError("feedback.conflict")
                state = "closed"
            elif kind == "reopen":
                if state not in FINAL:
                    raise UserFacingError("feedback.conflict")
                state, assignee = "new", None
                assignee_name = None
            else:
                raise UserFacingError("feedback.usage")
            row = self.conn.execute(
                "UPDATE feedback_ticket SET status = ?, assignee_id = ?, assignee_name = ?, "
                "version = version + 1, "
                "updated_at = ? WHERE number = ? AND version = ? RETURNING *",
                (state, assignee, assignee_name, now(), ticket.number, ticket.version),
            ).fetchone()
            if row is None:
                raise UserFacingError("feedback.conflict")
            result = Ticket(**dict(row))
            self._record(result, kind, actor, body, post_id, handlers, actor_name or actor)
            return result

    def due(self) -> list[Any]:
        # Preserve event order per recipient/ticket even when an earlier delivery is delayed.
        return list(
            self.conn.execute(
                "SELECT o.*, e.ticket_number, e.version FROM feedback_outbox o "
                "JOIN feedback_event e ON e.event_id = o.event_id "
                "WHERE o.state = 'pending' AND o.next_attempt_at <= ? AND NOT EXISTS ("
                "SELECT 1 FROM feedback_outbox older JOIN feedback_event prev "
                "ON prev.event_id = older.event_id WHERE older.state = 'pending' "
                "AND older.recipient_id = o.recipient_id AND prev.ticket_number = e.ticket_number "
                "AND prev.version < e.version) ORDER BY e.created_at, e.version LIMIT 50",
                (now(),),
            ).fetchall()
        )

    def delivered(self, outbox_id: str, *, skipped: bool = False) -> None:
        self.conn.execute(
            "UPDATE feedback_outbox SET state = ? WHERE outbox_id = ?",
            ("skipped" if skipped else "sent", outbox_id),
        )

    def retry(self, outbox_id: str, attempts: int) -> None:
        next_attempt = datetime.now(UTC) + timedelta(seconds=min(3600, 5 * 2 ** min(attempts, 10)))
        self.conn.execute(
            "UPDATE feedback_outbox SET attempts = attempts + 1, next_attempt_at = ? "
            "WHERE outbox_id = ?",
            (next_attempt.isoformat(), outbox_id),
        )
