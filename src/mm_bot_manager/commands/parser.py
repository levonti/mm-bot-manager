import shlex
from dataclasses import dataclass, field
from typing import Self


def parse_command(raw: str) -> list[str]:
    return shlex.split(raw, posix=True)


@dataclass(slots=True)
class ParsedArgs:
    positional: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_argv(cls, argv: list[str]) -> Self:
        positional: list[str] = []
        flags: dict[str, str] = {}
        i = 0
        while i < len(argv):
            tok = argv[i]
            if tok.startswith("--"):
                name = tok[2:]
                if "=" in name:
                    k, v = name.split("=", 1)
                    flags[k] = v
                elif i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                    flags[name] = argv[i + 1]
                    i += 1
                else:
                    flags[name] = ""
            else:
                positional.append(tok)
            i += 1
        return cls(positional=positional, flags=flags)

    def take_flag(self, *names: str) -> str | None:
        for n in names:
            if n in self.flags:
                return self.flags[n]
        return None
