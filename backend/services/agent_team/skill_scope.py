"""Skill workflow ceilings: unions within metadata, intersections over time.

This only narrows tools already admitted by the executor and runner. Shell
selectors match literal simple-command argv; they never authorize a shell
program containing substitutions, operators, redirects or extra commands.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from typing import Any

_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*\Z")
_ALIASES = {"Read": "read_file", "Bash": "run_command", "Shell": "run_command"}
_SHELL_SYNTAX = re.compile(r"[;&|<>$`\n\r\\(){}*?\[\]~]")


def _simple_argv(command: str) -> tuple[str, ...]:
    if (
        not isinstance(command, str)
        or not command.strip()
        or _SHELL_SYNTAX.search(command)
    ):
        raise ValueError("Skill shell selector requires a literal simple command")
    try:
        tokens = tuple(shlex.split(command))
    except ValueError as exc:
        raise ValueError("Invalid Skill command quoting") from exc
    if not tokens or "=" in tokens[0] or tokens[0].startswith("-"):
        raise ValueError("Invalid Skill command prefix")
    return tokens


def _selector(value: str) -> tuple[str, tuple[str, ...] | None, bool]:
    if _NAME.fullmatch(value):
        return _ALIASES.get(value, value), None, False
    match = re.fullmatch(r"(?:Bash|Shell)\((.+)\)", value)
    if not match:
        raise ValueError("Unsupported Skill allowed_tools selector")
    command = match.group(1)
    prefix = command.endswith((":*", " *"))
    if prefix:
        command = command[:-2]
    return "run_command", _simple_argv(command), prefix


def parse_allowed_tools(value: Any) -> frozenset[str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError("Invalid Skill allowed_tools JSON") from exc
    if not isinstance(value, list):
        raise ValueError("Invalid Skill allowed_tools")
    for item in value:
        if not isinstance(item, str):
            raise ValueError("Skill selectors must be strings")
        _selector(item)
    return frozenset(value)


@dataclass(frozen=True)
class SkillRestriction:
    # An empty clause denies every ordinary tool; no clauses means unrestricted.
    clauses: tuple[frozenset[str], ...] = ()

    @classmethod
    def from_metadata(cls, value: Any) -> SkillRestriction:
        selectors = parse_allowed_tools(value)
        return cls(()) if selectors is None else cls((selectors,))

    @classmethod
    def deny(cls) -> SkillRestriction:
        return cls((frozenset(),))

    def intersect(self, other: SkillRestriction) -> SkillRestriction:
        clauses = tuple(dict.fromkeys((*self.clauses, *other.clauses)))
        return SkillRestriction(clauses)

    def allows(self, tool: str, args: dict[str, Any] | None = None) -> bool:
        def matches(selector: str) -> bool:
            name, command, prefix = _selector(selector)
            if name != tool:
                return False
            if command is None or args is None:
                return True
            try:
                actual = _simple_argv(args.get("command"))
            except ValueError:
                return False
            return actual[: len(command)] == command if prefix else actual == command

        return all(
            any(matches(selector) for selector in clause) for clause in self.clauses
        )

    def to_data(self) -> list[list[str]]:
        return [sorted(clause) for clause in self.clauses]

    @classmethod
    def from_data(cls, value: Any) -> SkillRestriction:
        if not isinstance(value, list):
            raise ValueError("Invalid historical Skill scope")
        clauses = []
        for clause in value:
            selectors = parse_allowed_tools(clause)
            if selectors is None:
                raise ValueError("Invalid historical Skill clause")
            clauses.append(selectors)
        return cls(tuple(clauses))
