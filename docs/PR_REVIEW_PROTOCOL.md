# PR Review Output Protocol

> `<SAKURA_REVIEW>` tagged review output contract: envelope, validation, multi-round repair, and safe degradation.

← [Documentation Index](README.md) · [README](../README.md)

---

The main PR reviewer uses a line-oriented tagged protocol instead of JSON,
Markdown headings, score regexes, or severity emoji.

## Trust Boundary

The system message contains the review policy and output contract. Everything
obtained from a repository or another AI flow is untrusted evidence, including:

- PR text, diffs, code, file paths, comments, and linked issues
- generated PR summaries and previous review summaries
- `.sakura/` documents and memory
- tool results and external web content

Instructions inside that evidence must never change the output language,
protocol, severity, score, decision, or tool behavior. The configured output
language is normalized to `zh-CN` or `en`; invalid values fall back to `zh-CN`.
Only natural-language field contents are localized.

## Envelope

The model must return exactly one envelope with no surrounding text:

```text
<SAKURA_REVIEW>
<VERSION>1</VERSION>
<SCORE>1-10</SCORE>
<DECISION>approve|request_changes|comment</DECISION>
<DECISION_REASON>
Natural-language reason
</DECISION_REASON>
<SUMMARY>
Markdown summary
</SUMMARY>
<FINDINGS>
<FINDING>
<SEVERITY>critical|major|minor|suggestion</SEVERITY>
<FILE>repository/path|NONE</FILE>
<START_LINE>positive integer|NONE</START_LINE>
<END_LINE>positive integer|NONE</END_LINE>
<TITLE>
Short title
</TITLE>
<DESCRIPTION>
Evidence and impact
</DESCRIPTION>
<SUGGESTION>
Actionable fix|NONE
</SUGGESTION>
</FINDING>
</FINDINGS>
</SAKURA_REVIEW>
```

`FINDINGS` may be empty. `FILE=NONE` requires both line fields to be `NONE`.
A file finding requires both positive line values, with start not greater than
end. GitHub submission performs an additional diff safety-zone validation.
Every actionable item mentioned in `SUMMARY` must also be emitted as a
`FINDING`; Markdown file-and-line references in `SUMMARY` do not create inline
comments.

## Validation And Failure Handling

The parser validates field order, uniqueness, version, enums, score range,
required text, and file/line combinations. Critical findings cap the score at
3, and major findings cap it at 6.

The output contract still forbids surrounding text. As a provider-compatibility
measure, the parser deterministically extracts a single complete envelope from
an outer Markdown fence or short presentation preamble/epilogue. It rejects
responses containing zero or multiple envelopes and never infers findings from
surrounding prose.

The required output shape remains line-oriented block tags for
`DECISION_REASON`, `SUMMARY`, `TITLE`, `DESCRIPTION`, and `SUGGESTION`.
As a compatibility measure, the parser also accepts provider-style single-line
text fields such as `<SUGGESTION>NONE</SUGGESTION>`.

### Multi-round Repair Loop

An invalid response triggers a multi-round repair loop governed by
`protocol_repair_max_attempts` (configurable via WebUI, default 3). Each repair
round injects the specific validation error and preserves the full dialogue
history so the model can reformat the same conclusions. After each successful
repair, an `on_repaired` consistency check warns (without blocking) if the
finding count diverges from the original `<FINDING>` tag count. When all
attempts are exhausted, the loop degrades safely to a `comment` result with no
score or findings, preventing automatic approval or an accidental low-score
rejection.

The entire repair process is observable end-to-end via transcript / SSE /
observer attempts on the `review:protocol_repair` channel. The shared repair
loop (`backend/services/protocol_repair.py` → `run_protocol_repair_loop`) is
also reused by the Issue analysis protocol (`<SAKURA_ISSUE_ANALYSIS>`) and the
repository scan protocol. Review, analysis, and scan therefore share the same
configurable repair-attempt budget and safe degradation semantics.

The WebUI setting `review_timeout_seconds` is the single task deadline for PR
review, Issue analysis, and repository scanning. It is a soft deadline: an
in-flight provider call is allowed to return, and the next AI call receives one
user message requiring a final response from the information already collected,
with tools disabled. It does not restore a tool-round limit or hard-cancel the
task.

---

*Last updated: 2026-8-22 · Found an error? [Submit an Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
