# Davestigator — UI & Terminal Experience Specification v2.0

**Document Purpose:** Complete visual and architectural design specification for modernizing and elevating the user interface of the `Davestigator` CLI, its interactive components, and exportable reports.

---

## 1. Design System & Theme Foundations

### 1.1 Color Palette & Token Mapping

The visual language uses a high-contrast, modern dark-mode palette designed for developer and security auditor terminals.

| Semantic Token | Rich Color Spec | Terminal ANSI Fallback | Usage |
|---|---|---|---|
| `brand_primary` | `#38bdf8` (Sky Blue) | `bright_cyan` | Main application headers, command names, focus highlights |
| `brand_accent` | `#818cf8` (Indigo) | `bright_blue` | Category tags, system IDs, boundary markers |
| `status_complete` | `#22c55e` (Emerald) | `green` | Compliant states, satisfied obligations (`[✓] COMPLETE`) |
| `status_incomplete` | `#ef4444` (Crimson) | `red` | Policy violations, ghost data presence (`[!] INCOMPLETE`) |
| `status_na` | `#94a3b8` (Slate) | `dim white` | Non-deletion requests (`[⊘] NOT APPLICABLE`) |
| `finding_high` | `#dc2626` on `#fee2e2` | `white on red` | Critical findings (`REPORT_REMAINING_RECORD`, `EXPOSED_PII`) |
| `finding_risk` | `#d97706` on `#fef3c7` | `black on yellow`| Flow/Restore risks (`ACTIVE_DATA_FLOW`, `RESTORE_RISK`) |
| `timeline_claim` | `#0284c7` (Sky) | `cyan` | Claimed activities & workflow reports |
| `timeline_observed`| `#e11d48` (Rose) | `magenta` | Grounded facts from database/log evidence |
| `surface_border` | `#475569` (Slate Dark) | `bright_black` | Box borders (`box.ROUNDED`), table dividers |
| `text_muted` | `#64748b` (Muted Grey) | `dim` | Timestamps, file paths, supplementary footnotes |

### 1.2 Status Badges (Pills)

Status indicators are rendered as high-visibility text badge pills using pure typography (no emoji/unicode icons):

```text
[bold white on #16a34a]  COMPLETE  [/]
[bold white on #dc2626]  INCOMPLETE — GHOST DATA FOUND  [/]
[bold black on #cbd5e1]  NOT APPLICABLE  [/]
```

### 1.3 Typography & Text Badges (No Icons)

To guarantee consistent terminal column alignment and prevent rendering glitches across different terminal emulators and monospace fonts, all visual cues use pure text and ASCII tags rather than emoji/unicode icons:

| Concept | Tag / Label | Description |
|---|---|---|
| Verified / Compliant | `[PASS]` | Obligation satisfied by verified evidence |
| Policy Violation / Alert | `[FAIL]` / `[GHOST DATA DETECTED]` | Unpermitted data remains or ghost data found |
| Unresolved Gap / Inquiry | `[?]` | Unresolved question requiring inquiry |
| Stage Event Completed | `[DONE]` | Stage executed successfully |
| Data Formats | `CSV`, `SQL`, `LOG`, `JSON`, `PDF`, `PNG` | Clean uppercase format tags |

---

## 2. Screen-by-Screen Visual Wireframes

### 2.1 Application Welcome Screen (`Davestigator`)

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ 🛡️  DAVESTIGATOR — AI-Powered Data Deletion Auditor                    │
│    Forensic verification & ghost data discovery across distributed systems   │
╰─────────────────────────────────────────────────────────────────────────────╯

  Version:  0.1.0 (local engine + Ollama gemma3:4b)
  Security: READ-ONLY (destructions disabled)
  Config:   ./davestigator.yaml (Dataset: ./dataset)

  EXPLORATION & SETUP
    init                 Validate environment, directories, and configuration
    sources list         Inspect 6 core CSVs & 6 evidence format dialects
    scan                 Pre-parse all 94 evidence files & build fact index

  INVESTIGATION & AUDIT
    audit --case <ID>    Execute full 8-stage investigation on a single request
    audit --all          Batch audit all 518 cases & generate submission output.csv
    identity --case <ID> Trace user identity graph (current & historical)
    timeline --case <ID> View claim vs. evidence chronology bounded by audit_at
    findings --case <ID> Inspect confirmed policy violations & severities
    evidence --case <ID> Audit facts provenance & review excluded post-audit items

  REMEDIATION & COMPLIANCE
    plan --case <ID>     View ordered 3-phase remediation plan (PLAN ONLY)
    verify --case <ID>   Verify obligations (VERIFIED / FAILED / UNVERIFIABLE)
    report --case <ID>   Export audit report (--export json | markdown | html)
    validate-output      Verify output.csv against all 12 challenge contract rules

  Run 'Davestigator <command> --help' for command-specific flags.
```

---

### 2.2 Case Audit Screen (`Davestigator audit --case case_33`)

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ DELETION AUDIT: case_33                                      Mode: READ-ONLY│
├─────────────────────────────────────────────────────────────────────────────┤
│ Requester:   i***@outlook.com              Requested: 2026-07-16 08:00 UTC  │
│ User ID:     user_18 (Resolved via email)  Audit At:  2026-07-19 13:00 UTC  │
│ Reported:    COMPLETE                      Claimed:   2026-07-18 02:00 UTC  │
╰─────────────────────────────────────────────────────────────────────────────╯

INVESTIGATION PIPELINE
  [1/8] Load request ......................................... [✓ DONE]
  [2/8] Classify request intent (DELETION) ................... [✓ DONE]
  [3/8] Resolve identity graph (5 identifiers) ............... [✓ DONE]
  [4/8] Collect evidence & map resources (1 matched) ......... [✓ DONE]
  [5/8] Reconstruct timeline & clamp to audit_at ............. [✓ DONE]
  [6/8] Evaluate rules (deletion_rules.csv) .................. [✓ DONE]
  [7/8] Formulate remediation & verification plan ............ [✓ DONE]
  [8/8] Validate findings against visible proof .............. [✓ DONE]

AUDIT RESULT
  [bold white on #dc2626]  ✗ INCOMPLETE — GHOST DATA FOUND  [/]

CRITICAL FINDINGS
  ┌─ F-001 [bold white on red] REMAINING RECORD [/] ──────────────────────────────────────┐
  │ System:    export_service              Resource: export_1842              │
  │ Evidence:  evidence_18 (JSON export)   Rule ID:  rule_09 (DELETE)         │
  │ Severity:  HIGH                        State:    PRESENT                  │
  │ Details:   User data still persists in an export bundle after reported    │
  │            deletion. Immediate purge required.                            │
  └───────────────────────────────────────────────────────────────────────────┘

OBLIGATIONS SUMMARY
  [✓] 0 required records preserved as tombstones
  [!] 1 policy violation requires active remediation
  [?] 0 unresolved evidence gaps

NEXT INVESTIGATION ACTIONS
  Davestigator findings --case case_33
  Davestigator timeline --case case_33
  Davestigator plan     --case case_33
  Davestigator report   --case case_33 --export html
```

---

### 2.3 Identity Graph Tree Screen (`Davestigator identity --case case_33`)

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ IDENTITY RESOLUTION: case_33                                                │
╰─────────────────────────────────────────────────────────────────────────────╯

  Requested Input:   i***@outlook.com
  Resolution Status: [bold white on green] RESOLVED [/]
  Resolved Subject:  user_18

IDENTIFIER GRAPH
  user_18
  ├── ✉️  email (current)
  │   └── Value: i***@outlook.com           Source: user_identities.csv
  ├── 🔑  email_hash (derived)
  │   └── Value: 0649ed…                    Source: user_identities.csv (SHA-256)
  ├── 👤  name (current)
  │   └── Value: i. d.                      Source: user_identities.csv (masked)
  ├── 📞  phone (current)
  │   └── Value: ***30                      Source: user_identities.csv
  └── 🆔  user_ref (stable internal ID)
      └── Value: user_18                    Source: user_identities.csv

  * Note: Direct personal identifiers are masked. Names are never used alone to link records.
```

---

### 2.4 Forensic Multi-Lane Timeline (`Davestigator timeline --case case_33`)

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ FORENSIC TIMELINE: case_33                                                  │
╰─────────────────────────────────────────────────────────────────────────────╯

  Chronology of claimed events vs observed physical evidence (clamped to audit_at):

  TIMESTAMP (UTC)    SOURCE      TYPE         DETAILS
  ─────────────────  ──────────  ───────────  ────────────────────────────────────────────
  2026-07-16 08:00   Request     [REQUEST ]   Deletion requested by subject
  2026-07-18 02:00   Workflow    [CLAIM   ]   Deletion reported as COMPLETE
  2026-07-18 11:00   evidence_18 [OBSERVED]   export_1842 state observed PRESENT  ⚠️ GHOST
  2026-07-18 11:07   activity_40 [CLAIM   ]   LINK_RESOURCE_IDENTITY export_1842 (SUCCESS)
  2026-07-19 13:00   Boundary    [AUDIT_AT]   ═════════ AUDIT CUTOFF BOUNDARY ═════════

  FORENSIC CONCLUSION
  ⚠️ Discrepancy Found: Record was observed PRESENT in evidence_18 after the workflow
     claimed deletion was COMPLETE.
  ⚠️ The observed physical state supersedes claimed workflow results.
```

---

### 2.5 Remediation & Verification Plan Screen (`Davestigator plan --case case_33`)

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ REMEDIATION PLAN: case_33                                    Mode: PLAN ONLY│
├─────────────────────────────────────────────────────────────────────────────┤
│ No data has been modified. This plan represents the certified sequence of   │
│ actions required to bring the subject's data into full policy compliance.    │
╰─────────────────────────────────────────────────────────────────────────────╯

PHASE 1 — FINDINGS (Evidence-Backed Violations)
  Step 1. REPORT_REMAINING_RECORD → PRESENT
          System:   export_service
          Resource: export_1842
          Evidence: evidence_18

PHASE 2 — REMEDIATION (Targeted Actions to Execute)
  Step 2. DELETE_RECORD → ABSENT
          System:    export_service
          Resource:  export_1842
          Execution: [bold yellow]NOT PERFORMED (Pending Authorization)[/]

PHASE 3 — VERIFICATION (Post-Remediation Verification Matrix)
  Step 3. VERIFY_RECORD_ABSENT → ABSENT
          System:    export_service
          Resource:  export_1842
          Evidence:  [dim](Post-remediation run required)[/dim]

PLAN STATUS: [bold white on blue] READY FOR REVIEW [/]
Exported JSON: reports/case_33-plan.json
```

---

### 2.6 Full Batch Audit & Progress (`Davestigator audit --all`)

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│ FULL DATASET AUDIT (518 Cases)                                              │
╰─────────────────────────────────────────────────────────────────────────────╯

  Evaluating cases ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 518/518 [100%] (0:00:18)

AUDIT SUMMARY MATRIX
  ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
  ┃ Metric                      ┃ Count    ┃ Percentage                       ┃
  ┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
  │ Cases Processed             │ 518      │ 100.0%                           │
  │ [green]COMPLETE[/]                    │ 153      │ 29.5%                            │
  │ [red]INCOMPLETE[/]                  │ 355      │ 68.5%                            │
  │ [dim]NOT APPLICABLE[/]              │ 10       │ 1.9%                             │
  │ Processing Errors           │ 0        │ 0.0%                             │
  ├─────────────────────────────┼──────────┼──────────────────────────────────┤
  │ Guarded LLM Calls (Ollama)  │ 37       │ (Cached/Deterministic)           │
  │ Token Consumption           │ 24,591   │ 0 live (re-extraction bypassed)  │
  └─────────────────────────────┴──────────┴──────────────────────────────────┘

OUTPUT FILE VALIDATION
  [✓] Atomic write verified: output.csv (518 rows + header)
  [✓] Challenge Contract Validation: PASSED (12 of 12 rules verified)
```

---

## 3. Interactive Terminal Dashboard (TUI) Spec

Command: `Davestigator dashboard` (or `explore`)

### 3.1 Dual-Pane Layout

```text
┌─ Cases (518) ──────────────────────────┬─ Case Detail: case_33 ───────────────────────┐
│ Filter: [Incomplete (355)]  Search: _  │ [Overview]  Identity  Timeline  Plan  Report │
│                                        │                                              │
│ > case_33  [INCOMPLETE]  i***@outlook  │ Status:      INCOMPLETE (Ghost Data Found)   │
│   case_34  [COMPLETE]    h***@zoho.com │ Requester:   i***@outlook.com                │
│   case_35  [INCOMPLETE]  g***@gmail.co │ User ID:     user_18                         │
│   case_36  [COMPLETE]    a***@fastmail │ Audit Time:  2026-07-19 13:00:00 UTC         │
│   case_37  [COMPLETE]    a***@zoho.com │                                              │
│   case_38  [INCOMPLETE]  S***@GMAIL.CO │ VIOLATIONS (1)                               │
│   case_39  [COMPLETE]    j***@gmail.co │ • export_service / export_1842                │
│   case_40  [COMPLETE]    C***@GMAIL.CO │   (Observed PRESENT in evidence_18)          │
│   case_41  [INCOMPLETE]  l***@gmail.co │                                              │
│   case_42  [COMPLETE]    a***@gmail.co │ REMEDIATION REQUIRED                         │
│   case_43  [COMPLETE]    j***@outlook  │ 1. DELETE_RECORD on export_1842              │
│   case_44  [INCOMPLETE]  c***@outlook  │ 2. VERIFY_RECORD_ABSENT on export_1842       │
│   case_45  [COMPLETE]    o***@gmail.co │                                              │
├────────────────────────────────────────┴──────────────────────────────────────────────┤
│ [↑/↓] Navigate  [Tab] Switch Pane  [/] Search  [R] Export HTML  [V] Verify  [Q] Quit  │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 4. Standalone HTML Audit Report Spec

Command: `Davestigator report --case <ID> --export html`

### 4.1 HTML Architecture
- Single standalone `.html` file with embedded CSS and zero external JS/CDN requests.
- Dark theme with sleek glassmorphism panels, accessible typography (Inter / System UI), and print styles for PDF export.
- Key Sections:
  1. **Compliance Certificate Header**: Case ID, SHA-256 hash of decision, Audit Timestamp, and Signed Auditor ID.
  2. **Verdict Badge**: Large visual stamp (`COMPLETE`, `INCOMPLETE`, or `NOT_APPLICABLE`).
  3. **Identity Verification Block**: Table of linked identifiers and historical alias lineage.
  4. **Interactive Timeline Bar**: Color-coded SVG bar chart mapping events from request to audit cutoff.
  5. **Forensic Evidence Findings**: Expandable cards detailing raw evidence rows, snapshot replica comparisons, and violated rule descriptions.
  6. **Certified Remediation Checklist**: Printable table with signature blocks for compliance team execution.

---

## 5. Implementation Roadmap

| Priority | Component | Target Files | Key Changes |
|---|---|---|---|
| **P0** | **Rich Styling & Badges** | `code/davestigator/ui/console.py` | Add badge pills, rounded borders, theme colors, status mappings |
| **P0** | **Enhanced Screen Views** | `code/davestigator/cli.py` | Render identity tree, 3-lane timeline, card-based findings |
| **P1** | **HTML Report Generator** | `code/davestigator/reporting.py` | Add `export_html_report()` creating styled standalone HTML |
| **P2** | **Interactive TUI Dashboard** | `code/davestigator/ui/dashboard.py` | Implement Textual/Rich interactive split-pane case browser |
