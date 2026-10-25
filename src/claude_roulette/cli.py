"""claude-roulette: run several Claude subscription accounts side by side.

  add NAME [--primary]  log in once as that account and keep its OAuth credential
  remove NAME           forget an account and delete its stored credential
  status                show 5h-session and weekly usage for every account, and pinned conversations
  measure               estimate how cache reads, writes, input and output count toward the limits
  serve                 local proxy that keeps each conversation on one account + 07:00 ping
  run [-- ARGS]         exec `claude ARGS` through the proxy
  prime [--account N]   ping now (default: least-used account without an active window)
  install / uninstall   start `serve` + the menu bar app at every login (macOS LaunchAgents)
                        and point Claude Code and VS Code at the proxy
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer

from . import __version__, addons, client_settings, measure, science, service
from .config import (
    CONFIG_FILE,
    HOME,
    LOGIN_DIR,
    STATE_FILE,
    TOKENS_FILE,
    UPSTREAM,
    check_account_name,
    ensure_home,
    load_config,
    load_json,
    log,
    proxy_key,
    save_config_keys,
    save_json,
)
from .conversations import ConversationPins, status_lines
from .credentials import KEYCHAIN_SERVICE, _has_keychain, import_login, kc_delete
from .priming import claude_env, next_ping_text, prime_account, run_auto_prime, run_schedule
from .proxy import FORCE_HEADER, ProxyHandler, port_taken, proxy_status
from .router import Router
from .usage import Usage, caps, least_used, now, parse_ts, probe

# --------------------------------------------------------------------------- display

def fmt_reset(ts: datetime | None) -> str:
    if ts is None:
        return "-"
    secs = (ts - now()).total_seconds()
    if secs <= 0:
        return "expired"
    h, m = divmod(int(secs // 60), 60)
    return f"{h // 24}d{h % 24:02d}h" if h >= 24 else f"{h}h{m:02d}m"


def fmt_pct(p: float | None) -> str:
    return "-" if p is None else f"{p:5.1f}%"


def print_table(usages: list[Usage], cfg: dict, current: str | None = None,
                blocked: dict[str, float] | None = None, dead: dict[str, str] | None = None) -> None:
    router = Router(cfg)
    router.usages.update({u.name: u for u in usages})
    router.blocked_until = blocked or {}
    router.dead = dead or {}
    scores = router.scores([u.name for u in usages])
    print(f"  {'account':<12}{'5h used':>9}{'resets':>9}{'week used':>11}{'resets':>9}{'score':>8}  info")
    for u in usages:
        mark = "*" if u.name == current else " "
        info = cfg["emails"].get(u.name) or ""
        if u.name == cfg["primary"]:
            info += " (primary)"
        if u.name in cfg["account_caps"]:
            s_cap, w_cap = caps(u.name, cfg)
            info += f" (caps {s_cap:.0f}%/{w_cap:.0f}%)"
        if u.name in router.dead or u.error:
            print(f"{mark} {u.name:<12}  error: {router.dead.get(u.name) or u.error}")
            continue
        if u.name in scores:
            sc = f"{scores[u.name]:.1f}"
        elif router.blocked_until.get(u.name, 0) > time.time():
            sc = "429"
        else:
            sc = "full"
        print(f"{mark} {u.name:<12}{fmt_pct(u.session_pct):>9}{fmt_reset(u.session_reset):>9}"
              f"{fmt_pct(u.weekly_pct):>11}{fmt_reset(u.weekly_reset):>9}{sc:>8}  {u.source} {info}")


# --------------------------------------------------------------------------- CLI

def cmd_add(args, cfg):
    name = check_account_name(args.name)
    login_dir = LOGIN_DIR / name
    if login_dir.exists():
        shutil.rmtree(login_dir)
    login_dir.mkdir(parents=True)
    print(f"""
Adding account {name!r}. Claude Code will now start with an empty, temporary profile.
  1. Go through the first-run prompts and choose the Claude subscription login.
  2. In the browser, sign in as the account you want for {name!r}.
     (Already signed in as a different account? Copy the login URL into a private window.)
  3. Once Claude Code shows its prompt, type /exit.
Your normal ~/.claude login is not touched.
""")
    input("Press Enter to start Claude Code... ")
    subprocess.run([cfg["claude_bin"]], env=claude_env(CLAUDE_CONFIG_DIR=str(login_dir)), cwd=login_dir)

    email = import_login(name, login_dir)
    others = {n: e for n, e in cfg["emails"].items() if n != name}
    if email and email in others.values():
        print(f"warning: {email} is already stored under another name; was the right account used?")
    accounts = cfg["accounts"] + ([name] if name not in cfg["accounts"] else [])
    primary = name if args.primary or not cfg["primary"] else cfg["primary"]
    save_config_keys(accounts=accounts, primary=primary, emails={**cfg["emails"], name: email})
    print(f"stored {name} ({email or 'email unknown'}){' as primary' if primary == name else ''}")


def cmd_remove(args, cfg):
    if args.name not in cfg["accounts"]:
        sys.exit(f"unknown account {args.name!r}")
    if _has_keychain():
        kc_delete(KEYCHAIN_SERVICE, args.name)
    else:
        tokens = load_json(TOKENS_FILE, {})
        tokens.pop(args.name, None)
        save_json(TOKENS_FILE, tokens)
    accounts = [a for a in cfg["accounts"] if a != args.name]
    emails = {k: v for k, v in cfg["emails"].items() if k != args.name}
    primary = cfg["primary"] if cfg["primary"] != args.name else (accounts[0] if accounts else None)
    save_config_keys(accounts=accounts, emails=emails, primary=primary)
    print(f"removed {args.name}")


def cmd_status(args, cfg):
    mismatch = service.service_mismatch()
    if mismatch:
        print(f"warning: {mismatch}; run `claude-roulette install` again to update it\n")
    state = load_json(STATE_FILE, {})
    proxy = proxy_status(cfg)
    if proxy == "other":
        print(f"warning: {port_taken(cfg)}\n")
    if proxy == "ours" and state.get("usages"):
        usages = [Usage(**{**u, "session_reset": parse_ts(u["session_reset"]),
                           "weekly_reset": parse_ts(u["weekly_reset"])})
                  for u in state["usages"].values()]
        print(f"proxy on :{cfg['proxy_port']}, state from {state['updated_at']}")
        print_table(usages, cfg, state.get("current"), state.get("blocked_until"), state.get("dead"))
        for line in status_lines(state.get("conversations") or {}):
            print(line)
        for name, addon in addons.load().items():
            for line in addon.status((state.get("addons") or {}).get(name) or {}):
                print(line)
    else:
        print("proxy not running, probing directly")
        print_table([probe(a, cfg) for a in cfg["accounts"]], cfg)


def cmd_serve(args, cfg):
    if not cfg["accounts"]:
        sys.exit("no accounts configured; run `add` first")
    ensure_home()
    router = Router(cfg)
    for addon in addons.load().values():
        addon.start(router)
    ProxyHandler.router = router
    ProxyHandler.pins = pins = ConversationPins(router)
    ProxyHandler.upstream = urllib.parse.urlsplit(UPSTREAM)
    ProxyHandler.key = proxy_key()
    ProxyHandler.debug = args.debug
    try:
        server = ThreadingHTTPServer(("127.0.0.1", cfg["proxy_port"]), ProxyHandler)
    except OSError as e:
        if proxy_status(cfg) == "other":
            log(f"warning: {port_taken(cfg)}")
        sys.exit(f"cannot listen on 127.0.0.1:{cfg['proxy_port']} ({e}); is another `serve` running?")
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"proxy on http://127.0.0.1:{cfg['proxy_port']} -> {UPSTREAM}, "
        f"accounts {cfg['accounts']}, primary {cfg['primary']}, add-ons {list(addons.load()) or 'none'}")

    state = load_json(STATE_FILE, {})
    state.update(pid=os.getpid(), started_at=now().isoformat(timespec="seconds"),
                 command=service.cli_command(), version=__version__)
    last_probe = 0.0
    last_tick = time.time()
    while True:
        gap = time.time() - last_tick
        if gap > max(4 * args.tick, 120):  # the loop was frozen: the Mac slept
            log(f"resumed after {gap / 60:.0f} min (sleep/wake), refreshing usage")
            last_probe = 0.0
        last_tick = time.time()

        if time.time() - last_probe >= cfg["probe_interval_s"]:
            transient_error = False
            for a in cfg["accounts"]:
                u = probe(a, cfg)
                if u.source == "dead":
                    router.mark_dead(a, u.error)
                elif u.error:
                    transient_error = True
                    log(f"probe {a}: {u.error}")
                router.merge_probe(u)
            last_probe = time.time()
            if transient_error:  # e.g. Wi-Fi not back yet after wake: retry in a minute
                last_probe -= max(cfg["probe_interval_s"] - 60, 0)
        usages = [router.usages[a] for a in cfg["accounts"] if a not in router.dead]
        run_schedule(cfg, usages, state)
        run_auto_prime(cfg, usages, state)
        state.update(router.snapshot(), accounts=cfg["accounts"], primary=cfg["primary"],
                     emails=cfg["emails"], next_ping=next_ping_text(cfg, state),
                     conversations=pins.snapshot(),
                     addons={n: a.snapshot() for n, a in addons.load().items()})
        save_json(STATE_FILE, state)
        time.sleep(args.tick)


def cmd_measure(args, cfg):
    for line in measure.report(measure.load_rows(), min_samples=args.min_samples):
        print(line)


def cmd_run(args, cfg):
    claude_args = args.claude_args[1:] if args.claude_args[:1] == ["--"] else args.claude_args
    if args.account and args.account not in cfg["accounts"]:
        sys.exit(f"unknown account {args.account!r} (configured: {', '.join(cfg['accounts'])})")
    proxy = proxy_status(cfg)
    if proxy == "other":
        sys.exit(port_taken(cfg))
    if proxy == "ours":
        env = claude_env(ANTHROPIC_BASE_URL=client_settings.proxy_url(cfg))
        if args.account:
            env["ANTHROPIC_CUSTOM_HEADERS"] = f"{FORCE_HEADER}: {args.account}"
            log(f"routing through proxy, pinned to {args.account}")
        else:
            log("routing through proxy (account chosen per request)")
    else:
        if args.account:
            sys.exit("--account needs the proxy; start `serve` first")
        env = claude_env()
        log("proxy not running: starting Claude Code on its own login, no rotation")
    os.execvpe(cfg["claude_bin"], [cfg["claude_bin"], *claude_args], env)


def cmd_prime(args, cfg):
    if args.account:
        name = args.account
    else:
        u = least_used([probe(a, cfg) for a in cfg["accounts"]], cfg)
        if u is None:
            sys.exit("no eligible account")
        if u.window_active:
            print(f"note: {u.name} already has an active window")
        name = u.name
    ok, msg = prime_account(name, cfg)
    print(f"{name}: {'ok' if ok else 'FAILED'} {msg}")


def enable_detected_addons() -> str | None:
    """Turn on the Science add-on if Claude Science is installed and `addons` was never set."""
    if "addons" in load_json(CONFIG_FILE, {}) or not science.app_installed():
        return None
    save_config_keys(addons=["science"])
    return 'found Claude Science: enabled the science add-on (set "addons": [] in config.json to turn it off)'


def cmd_install(args, cfg):
    if sys.platform != "darwin":
        sys.exit("install is macOS-only; run `serve` under systemd or similar elsewhere")
    if not cfg["accounts"]:
        sys.exit("no accounts configured; run `add` first")
    if line := enable_detected_addons():  # before the service starts, which reads config.json once
        print(line)
    agents = {service.SERVE_LABEL: service.agent_plist(
        service.SERVE_LABEL, [*service.cli_command(), "serve"], True, HOME / "serve.log")}
    if not args.no_menubar:
        print("building the menu bar app…")
        service.build_app()
        # Restart only if it crashed; choosing Quit keeps it closed until the next login.
        agents[service.BAR_LABEL] = service.agent_plist(
            service.BAR_LABEL, [str(service.APP_EXE)], {"SuccessfulExit": False}, None)
    if proxy_status(cfg) != "down":  # ours, or an older version that can't prove it yet
        service.bootout(service.SERVE_LABEL)
        time.sleep(1)
        if proxy_status(cfg) != "down":
            sys.exit(f"port {cfg['proxy_port']} is in use; stop the `serve` you started by hand first "
                     f"(find it with `lsof -nP -iTCP:{cfg['proxy_port']} -sTCP:LISTEN`)")
    for label, plist in agents.items():
        print(f"loaded {service.write_agent(label, plist)}")
    for line in client_settings.connect_clients(cfg, claude=not args.no_claude_settings,
                                                vscode=not args.no_vscode_settings):
        print(line)
    print("installed: the proxy and menu bar app now start at every login.")


def cmd_uninstall(args, cfg):
    service.remove_agents()
    for line in client_settings.disconnect_clients(cfg):
        print(line)
    print("removed the LaunchAgents and the menu bar app (credentials and config are kept;"
          " use `remove NAME` to delete an account)")


def main(argv=None):
    p = argparse.ArgumentParser(prog="claude-roulette", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--primary", action="store_true")
    a.set_defaults(fn=cmd_add)
    rm = sub.add_parser("remove")
    rm.add_argument("name")
    rm.set_defaults(fn=cmd_remove)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    ms = sub.add_parser("measure", help="estimate how much each token type counts toward the limits")
    ms.add_argument("--min-samples", type=int, default=30)
    ms.set_defaults(fn=cmd_measure)
    s = sub.add_parser("serve")
    s.add_argument("--tick", type=float, default=30, help="seconds between scheduler checks")
    s.add_argument("--debug", action="store_true", help="log every rate-limit header received")
    s.set_defaults(fn=cmd_serve)
    r = sub.add_parser("run")
    r.add_argument("--account", help="pin this session to one account (needs the proxy)")
    r.add_argument("claude_args", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)
    pr = sub.add_parser("prime")
    pr.add_argument("--account")
    pr.set_defaults(fn=cmd_prime)
    ins = sub.add_parser("install")
    ins.add_argument("--no-menubar", action="store_true", help="only install the background service")
    ins.add_argument("--no-claude-settings", action="store_true",
                     help=f"don't set env.{client_settings.PROXY_VAR} in ~/.claude/settings.json")
    ins.add_argument("--no-vscode-settings", action="store_true",
                     help=f"don't set {client_settings.VSCODE_KEY} in the VS Code user settings")
    ins.set_defaults(fn=cmd_install)
    sub.add_parser("uninstall").set_defaults(fn=cmd_uninstall)
    args = p.parse_args(argv)
    args.fn(args, load_config())

