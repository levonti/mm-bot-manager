from mm_bot_manager.commands.parser import ParsedArgs, parse_command


def test_parse_simple():
    argv = parse_command("create demo-bot")
    assert argv == ["create", "demo-bot"]


def test_parse_quoted():
    argv = parse_command('create demo-bot --display-name "Demo Bot"')
    parsed = ParsedArgs.from_argv(argv[1:])
    assert parsed.positional == ["demo-bot"]
    assert parsed.flags == {"display-name": "Demo Bot"}


def test_parse_equals_form():
    argv = parse_command("update demo --description=short")
    parsed = ParsedArgs.from_argv(argv[1:])
    assert parsed.positional == ["demo"]
    assert parsed.flags == {"description": "short"}


def test_parse_multiple_flags():
    argv = parse_command("create x --display-name Demo --description hello")
    parsed = ParsedArgs.from_argv(argv[1:])
    assert parsed.positional == ["x"]
    assert parsed.flags == {"display-name": "Demo", "description": "hello"}


def test_take_flag_aliases():
    p = ParsedArgs(positional=[], flags={"description": "d"})
    assert p.take_flag("desc", "description") == "d"
    assert p.take_flag("missing") is None
