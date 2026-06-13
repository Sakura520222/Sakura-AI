# PR Review Output Protocol

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

An invalid first response triggers one format-only retry at temperature zero,
without tools. The original assistant response is included so the model can
reformat the same conclusions. A second invalid response produces a safe
`comment` result with no score or findings, preventing automatic approval or an
accidental low-score rejection.
