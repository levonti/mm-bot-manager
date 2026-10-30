"""Exercise EN/RU/DE against a disposable local Mattermost, never the configured server.

Requires Docker and the pinned amd64 Mattermost image. No .env files are loaded.
Only a sanitized report is persisted; containers, their volumes and the isolated
application database are discarded. Run with Python in the project's uv environment.
"""

import argparse
import asyncio
import contextlib
import json
import os
import secrets
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from mm_bot_manager.__main__ import serve
from mm_bot_manager.config import Settings
from mm_bot_manager.db import connect
from mm_bot_manager.feedback import FeedbackRepo
from mm_bot_manager.i18n import catalog, tr
from mm_bot_manager.logging import configure_logging
from mm_bot_manager.mm_client import MattermostClient
from mm_bot_manager.repository import PreferenceRepo

IMAGE = (
    "mattermost/mattermost-team-edition:10.11.24"
    "@sha256:956002f81635864b873239ee83042a803167f0fb452348fbea72b2142d7c89fc"
)
LANGUAGES = ("en", "ru", "de")


def docker(*args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, env=env, check=False, timeout=90
    )
    if result.returncode:
        # Docker diagnostics can contain configured environment values. Never echo them.
        raise RuntimeError(f"Docker operation failed: {args[0]}")
    return result.stdout.strip()


async def exercise(report: dict, root: Path, url: str) -> None:
    configure_logging("ERROR")
    task = None
    conn = None
    clients = []
    report["stage"] = "server readiness"
    async with (
        contextlib.AsyncExitStack() as stack,
        httpx.AsyncClient(base_url=url + "/api/v4", trust_env=False, timeout=15) as raw,
    ):
        for _ in range(120):
            try:
                response = await raw.get("/system/ping", timeout=2)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise TimeoutError("Mattermost readiness")
        report["server_version"] = ".".join(
            response.headers.get("X-Version", "not exposed").split(".")[0:3]
        )
        report["stage"] = "test identities"
        password = secrets.token_urlsafe(32)

        async def create_user(username: str, language: str) -> dict:
            response = await raw.post(
                "/users",
                json={
                    "email": f"{username}@example.test",
                    "username": username,
                    "password": password,
                    "locale": language,
                },
            )
            response.raise_for_status()
            return response.json()

        admin_user = await create_user("acceptance-admin", "en")
        response = await raw.post(
            "/users/login", json={"login_id": admin_user["username"], "password": password}
        )
        response.raise_for_status()
        session = await stack.enter_async_context(
            MattermostClient(url + "/api/v4", response.headers["Token"])
        )
        clients.append(session)
        access = await session.create_user_access_token(admin_user["id"], "isolated acceptance")
        admin_token = access["token"]
        admin = await stack.enter_async_context(MattermostClient(url + "/api/v4", admin_token))
        clients.append(admin)
        manager = await admin.create_bot(
            username="acceptance-manager", display_name="Language test"
        )
        token = await admin.create_user_access_token(manager["user_id"], "isolated manager")
        actors = {}
        for language in LANGUAGES:
            user = await create_user(f"acceptance-{language}", language)
            access = await admin.create_user_access_token(user["id"], "isolated participant")
            client = await stack.enter_async_context(
                MattermostClient(url + "/api/v4", access["token"])
            )
            clients.append(client)
            channel = await client.create_direct_channel(user["id"], manager["user_id"])
            actors[language] = {"user": user, "client": client, "channel": channel["id"]}
        cfg = Settings(
            _env_file=None,
            mm_url=url,
            mm_token=admin_token,
            mm_bot_token=token["token"],
            db_url=None,
            db_host=None,
            db_path=str(root / "acceptance.sqlite"),
            mm_approvers=",".join(a["user"]["username"] for a in actors.values()),
            mm_feedback_handlers=",".join(a["user"]["username"] for a in actors.values()),
            mm_default_language="en",
            mm_display_timezone="Europe/Moscow",
            health_port=8080,
        )
        # Settings validates ordinary ports; 0 is used only for this ephemeral test listener.
        cfg.health_port = 0
        conn = connect(cfg.db_path)
        task = asyncio.create_task(serve(cfg, conn))
        observed = set()

        def passed(name: str) -> None:
            report["checks"].append({"name": name, "result": "PASS"})
            print("PASS", name, flush=True)

        async def wait_post(language: str, predicate, after: int = 0) -> dict:
            actor = actors[language]
            deadline = asyncio.get_running_loop().time() + 25
            while asyncio.get_running_loop().time() < deadline:
                if task.done():
                    await task
                    raise RuntimeError("Manager exited")
                posts = await actor["client"]._request(
                    "GET", f"/channels/{actor['channel']}/posts?per_page=100"
                )
                for pid in reversed(posts["order"]):
                    post = posts["posts"][pid]
                    if (
                        pid not in observed
                        and post["user_id"] == manager["user_id"]
                        and post["create_at"] >= after
                        and predicate(post["message"])
                    ):
                        observed.add(pid)
                        return post
                await asyncio.sleep(0.1)
            raise TimeoutError("Expected localized response not received")

        async def command(language: str, text: str, expected: str) -> dict:
            report["stage"] = f"{language}: {text.split()[0]}"
            actor = actors[language]
            sent = await actor["client"].create_post(channel_id=actor["channel"], message=text)
            return await wait_post(language, lambda body: expected in body, sent["create_at"])

        try:
            # Wait for the real WebSocket to authenticate, without replacing its listener.
            await asyncio.sleep(3)
            for language in LANGUAGES:
                reply = await command(language, "!help", tr(language, "help.text"))
                assert "!language [ru|en|de|auto]" in reply["message"]
                passed(f"{language}: profile auto detection and role help over WebSocket")
                for choice in LANGUAGES:
                    expected = tr(
                        choice,
                        "language.changed",
                        preference=tr(choice, f"language.{choice}"),
                        effective=tr(choice, f"language.{choice}"),
                    )
                    await command(language, f"!language {choice}", expected)
                    assert PreferenceRepo(conn).get(actors[language]["user"]["id"])[0] == choice
                    await command(language, "!help", tr(choice, "help.text"))
                    passed(f"{language}: manual {choice} and immediate help")
                expected = tr(
                    language,
                    "language.changed",
                    preference=tr(language, "language.auto"),
                    effective=tr(language, f"language.{language}"),
                )
                await command(language, "!language auto", expected)
                passed(f"{language}: auto restores server profile")
                cases = [
                    ("!language fr", tr(language, "language.usage")),
                    (
                        "!nonesuch",
                        tr(
                            language, "__init__.unknown_command_send_help_for_a_list", v0="nonesuch"
                        ),
                    ),
                    ('!create "unclosed', tr(language, "__init__.could_not_parse_command")),
                    ("!create invalid-name", tr(language, "validation.username_suffix")),
                ]
                for text, expected in cases:
                    await command(language, text, expected)
                assert PreferenceRepo(conn).get(actors[language]["user"]["id"])[0] == "auto"
                passed(
                    f"{language}: invalid language, unknown command, parser and validation errors"
                )

            await asyncio.gather(
                *(command(language, "!help", tr(language, "help.text")) for language in LANGUAGES)
            )
            passed("concurrent EN/RU/DE help remains isolated per user")
            for language in LANGUAGES:
                reply = await command(
                    language,
                    "!import nonexistent-bot --repo https://example.test/project --criticality 2",
                    catalog(language)["error.api_diagnostic"].split("{")[0],
                )
                assert "404" in reply["message"]
                assert "api.user.get_by_username" not in reply["message"]
                passed(f"{language}: real HTTP 404 is localized with safe diagnostic")

            de_id = actors["de"]["user"]["id"]
            await admin._request("PUT", f"/users/{de_id}/patch", json={"locale": "fr"})
            await command(
                "de",
                "!language auto",
                tr(
                    "en",
                    "language.changed",
                    preference=tr("en", "language.auto"),
                    effective=tr("en", "language.en"),
                ),
            )
            await command("de", "!help", tr("en", "help.text"))
            await command(
                "de",
                "!language de",
                tr(
                    "de",
                    "language.changed",
                    preference=tr("de", "language.de"),
                    effective=tr("de", "language.de"),
                ),
            )
            await command("de", "!help", tr("de", "help.text"))
            await admin._request("PUT", f"/users/{de_id}/patch", json={"locale": "de"})
            await command(
                "de",
                "!language auto",
                tr(
                    "de",
                    "language.changed",
                    preference=tr("de", "language.auto"),
                    effective=tr("de", "language.de"),
                ),
            )
            passed("unsupported profile falls back to EN; explicit DE overrides profile")

            # One request from each real human test user; every recipient gets their own language.
            for index, author in enumerate(LANGUAGES):
                name = f"acceptance-{author}-bot"
                await command(
                    author,
                    f"!create {name} --repo https://example.test/project --criticality 2",
                    catalog(author)["create_bot.your_request_to_create_has_been_submitted"].split(
                        "{"
                    )[0],
                )
                row = conn.execute(
                    "SELECT request_id FROM pending_bot_request WHERE username = ?", (name,)
                ).fetchone()
                request_id = row[0]
                for recipient in LANGUAGES:
                    post = await wait_post(
                        recipient, lambda body, rid=request_id: rid in body and "!approve" in body
                    )
                    assert (
                        tr(
                            recipient,
                            "notice.creation",
                            request_id=request_id,
                            requester=actors[author]["user"]["username"],
                            username=name,
                            display_name="—",
                            description="—",
                            repository="https://example.test/project",
                            criticality=f"2 ({tr(recipient, 'criticality.2')})",
                        )
                        == post["message"]
                    )
                    passed(f"{author}->{recipient}: approval request notification")
                await command(
                    author, f"!approve {request_id}", tr(author, "approve.self_forbidden")
                )
                reviewer = LANGUAGES[(index + 1) % 3]
                reason = "Keep Unicode: Grüße / Привет / hello"
                await command(
                    reviewer,
                    f"!deny {request_id} {reason}",
                    tr(reviewer, "deny.denied_request_for", v0=request_id, v1=name),
                )
                notice = await wait_post(
                    author, lambda body, rid=request_id, why=reason: rid in body and why in body
                )
                assert (
                    tr(
                        author,
                        "notice.denied",
                        kind=tr(author, "kind.create"),
                        username=name,
                        approver=actors[reviewer]["user"]["username"],
                        request_id=request_id,
                        reason=tr(author, "notice.reason", reason=reason),
                    )
                    == notice["message"]
                )
                passed(f"{reviewer}->{author}: denial notification preserves reason")

            feedback = FeedbackRepo(conn)
            for index, author in enumerate(LANGUAGES):
                text = f"Acceptance {author}: Grüße / Привет / hello"
                # Determine ticket number through the reply, without assuming another actor's state.
                await command(
                    author,
                    "!feedback new " + text,
                    tr(author, "feedback.created", ref=f"FB-{index + 1}"),
                )
                ref = f"FB-{index + 1}"
                for recipient in LANGUAGES:
                    if recipient == author:
                        continue
                    post = await wait_post(
                        recipient,
                        lambda body, ticket=ref, message=text: ticket in body and message in body,
                    )
                    assert tr(recipient, "feedback.kind.new") in post["message"]
                    assert tr(recipient, "feedback.state.new") in post["message"]
                    assert tr(recipient, "feedback.actions", ref=ref) in post["message"]
                    passed(f"{author}->{recipient}: durable feedback notification")
                handler = LANGUAGES[(index + 1) % 3]
                await command(
                    handler, f"!feedback take {ref}", tr(handler, "feedback.saved", ref=ref)
                )
                post = await wait_post(
                    author,
                    lambda body, ticket=ref: ticket in body and ("#2" in body or "№2" in body),
                )
                assert tr(author, "feedback.actions", ref=ref) in post["message"]
                assert "UTC+03:00" in post["message"]
                assert feedback.get(index + 1).assignee_id == actors[handler]["user"]["id"]
                passed(f"{handler}->{author}: assignment notification and timezone")

            report["stage"] = "restart persistence"
            # Reopen the SQLite file and restart the complete manager with saved choices.
            for language in LANGUAGES:
                await command(
                    language,
                    f"!language {language}",
                    tr(
                        language,
                        "language.changed",
                        preference=tr(language, f"language.{language}"),
                        effective=tr(language, f"language.{language}"),
                    ),
                )
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            conn.close()
            conn = connect(cfg.db_path)
            task = asyncio.create_task(serve(cfg, conn))
            await asyncio.sleep(3)
            for language in LANGUAGES:
                await command(language, "!help", tr(language, "help.text"))
                assert PreferenceRepo(conn).get(actors[language]["user"]["id"])[0] == language
                passed(f"{language}: preference and help survive full manager restart")
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if conn:
                conn.close()
            for client in clients:
                await client.aclose()


async def main(output: Path) -> int:
    run_id = uuid4().hex[:10]
    prefix = "mm-language-" + run_id
    network = prefix + "-net"
    database = prefix + "-db"
    server = prefix + "-server"
    report = {
        "date": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "image": IMAGE,
        "environment": "disposable localhost Mattermost with dedicated human accounts",
        "checks": [],
        "result": "FAIL",
        "cleanup": False,
    }
    created = []
    network_created = False
    try:
        docker("network", "create", network)
        network_created = True
        env = dict(os.environ)
        env["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
        env["MM_SQLSETTINGS_DATASOURCE"] = (
            f"postgres://postgres:{env['POSTGRES_PASSWORD']}@{database}:5432/mattermost?sslmode=disable"
        )
        created.append(database)
        docker(
            "run",
            "-d",
            "--name",
            database,
            "--network",
            network,
            "-e",
            "POSTGRES_PASSWORD",
            "-e",
            "POSTGRES_DB=mattermost",
            "postgres:15-alpine",
            env=env,
        )
        created.append(server)
        docker(
            "run",
            "-d",
            "--name",
            server,
            "--network",
            network,
            "--platform",
            "linux/amd64",
            "-p",
            "127.0.0.1::8065",
            "-e",
            "MM_SQLSETTINGS_DRIVERNAME=postgres",
            "-e",
            "MM_SQLSETTINGS_DATASOURCE",
            "-e",
            "MM_SERVICESETTINGS_ENABLEUSERACCESSTOKENS=true",
            "-e",
            "MM_SERVICESETTINGS_ENABLEBOTACCOUNTCREATION=true",
            "-e",
            "MM_TEAMSETTINGS_ENABLEOPENSERVER=true",
            "-e",
            "MM_EMAILSETTINGS_REQUIREEMAILVERIFICATION=false",
            "-e",
            "MM_PLUGINSETTINGS_ENABLE=false",
            "-e",
            "MM_LOGSETTINGS_CONSOLELEVEL=ERROR",
            IMAGE,
            env=env,
        )
        port = docker("port", server, "8065").rsplit(":", 1)[1]
        with tempfile.TemporaryDirectory(prefix="mm-language-") as directory:
            await exercise(report, Path(directory), "http://127.0.0.1:" + port)
        report["result"] = "PASS"
    except Exception as exc:
        # Do not serialize arbitrary API/Docker exceptions or tracebacks with runtime data.
        report["failure_type"] = type(exc).__name__
        print("FAIL", report.get("stage", "provisioning"), type(exc).__name__, flush=True)
    finally:
        cleanup_ok = True
        for container in reversed(created):
            try:
                docker("rm", "-f", "-v", container)
            except Exception:
                cleanup_ok = False
        if network_created:
            try:
                docker("network", "rm", network)
            except Exception:
                cleanup_ok = False
        report["cleanup"] = cleanup_ok
        if not cleanup_ok:
            report["result"] = "FAIL_CLEANUP"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("RESULT", report["result"], "checks", len(report["checks"]), flush=True)
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Sanitized JSON report path")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.output)))
