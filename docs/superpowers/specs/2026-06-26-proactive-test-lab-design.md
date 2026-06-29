# Spec: Proactive Test Lab Message Moderation Desk [Option A]

*   Date: 2026-06-26
*   Status: Approved
*   Branch: codex/proactive-test-lab-wip
*   Target Path: /Users/xueyuqi/code/ai4all_bridge/app/static/proactive_test_lab.html

---

## 1. Executive Summary

This spec details the UI/UX refactoring and interface refinement of the Proactive Test Lab [主动消息评估测试台]. 
Operating strictly in Option A [线下大模型评估评测台], this page is dedicated to non-production offline testing, scenario generation evaluation, L0-L4 model reasoning inspections, and high-fidelity human feedback/review labeling. It serves as a tool for engineering teams to assess prompt performance, identify content risks, and curate high-quality few-shot training corpora.

---

## 2. Key User Journeys

1.  Context Seeding / Sample Capture:
    *   Reviewer uses the Session Importer inside the top panel by entering an account_id and optional session_id, fetching up to 120 messages from actual desensitized chat logs.
    *   Alternatively, import a .jsonl file or use pre-populated synthetic seed sets.
2.  Dry-Run Dry-Fire [Batch Generation]:
    *   Reviewer triggers candidate generation using a specific run configuration. This prompts the background LLM to produce candidate messages and output full L0-L4 reasoning pipelines without doing any actual WeChat dispatch.
3.  Audit Card Dispatch:
    *   The list of newly generated candidates populates the Left Queue. Reviewer selects a card. State is synced immediately across columns.
4.  Evidence-Based Cross-Verification:
    *   In the Center Workspace, the reviewer inspects the long-scroll chat history, notes any open loops, and inspects the multi-layer output [L0 Context, L1 Trigger, L2 When, L3 How, L4 Safety].
5.  Multi-Dimensional Human Annotation:
    *   In the Right Form, the reviewer votes to promote [human_should_promote].
    *   If promoted, reviewer refines the raw draft in Revised Message, scores tones, selects promotion tier [keep, fewshot, rule, auto_send], and flags anticipated user reception [accepted, ignored, etc.].
    *   If rejected, the reviewer flags the exact failure mode [too_pushy, hallucinated_fact, etc.] and documents notes.
6.  Hotkey Actions and Next Card Auto-Focus:
    *   Pressing Ctrl + Enter [or Cmd + Enter] commits the review via POST /internal/proactive-test/reviews. The card transitions from Pending to Approved/Rejected, and focus automatically advances to the next item in the Left Queue.

---

## 3. Screen Layout

The workspace occupies a single full height screen [100vh, overflow: hidden] and is split into three main panels with distinct scroll zones:

```
+----------------------------------------------------------------------------------------------------+
|  [Header Actions and Metrics Zone]                                                                   |
+----------------------------------------------------------------------------------------------------+
|  LEFT QUEUE [320px]        |  CENTER WORKSPACE [Flex-grow, min-width: 460px] | RIGHT PANEL [340px] |
|  - Filters and Run Selector  |  - Sample Context Block                         | - Review Form       |
|  - Scrollable Cards list   |  - Chat Timeline Explorer                       | - Hotkeys Legend    |
|                            |  - L0-L4 Step-Down Reasoning Blocks             |                     |
+----------------------------------------------------------------------------------------------------+
```

---

## 4. Technical Component Specs

### 4.1 Left Column: The Queue Panel
*   Run ID / Status filter: Group candidates by execution run ID or filters [All, Pending, Approved, Rejected, Skip].
*   Candidate Card:
    *   Displays sample_id, a badge for scenario_type [color-coded: reactivation_topic is purple, account_check is indigo, content_invitation is teal].
    *   Shows raw text snippet of the generated message.
    *   Displays L2 Intervene score [-2 to 2]. If score is positive, display a green badge; if score is negative or zero, display a red or gray warning.
    *   Shows checkmark badge if human reviewed.

### 4.2 Center Column: Context and Reasoning
*   Sample Metadata Card: Shows open_loop constraints, natural language context background, and target objectives.
*   WeChat Chat Log Explorer:
    *   A compact, scrollable vertical sequence showing past conversation.
    *   User speech bubbles are on the left [light gray], AI speech bubbles on the right [faint brand color].
*   L0-L4 Multi-Tier Panel:
    *   L0 Context Block: Displays desensitized profile traits, user energy level, and memory assets.
    *   L1 Trigger Block: Highlights why this interaction was triggered, showcasing the matched trigger type.
    *   L2 Intervention Score Gauge: Displays a colored meter showing should-intervene status.
    *   L3 Dialogue Generation Guide: Shows target planners, grounding directives, and tone balances.
    *   L4 Safety Risk Checklist: Flags violations [sensitive_topic, wrong_memory, too_intimate, etc.].

### 4.3 Right Column: Human Labeler Form
*   Action Toggles:
    *   Promotion switch: "Promote / Approve [晋级]" vs "Reject / Skip [拦截]".
    *   reject_reason Select Box: Displayed only if rejected.
*   Sliders and Scores:
    *   Tone Score [1-5 slider or radio group]
    *   Pressure Score [1-5 slider or radio group]
    *   Marketing Score [1-5 slider or radio group]
*   Risks and Policy:
    *   Checkbox for privacy_risk and hallucination_risk.
    *   Promotion Destination Select: keep, fewshot [for training], rule, auto_send.
    *   Expectation Select: accepted, ignored, negative, opt-out.
*   Draft Area:
    *   Large textarea containing revised_message, pre-filled with the candidate message. Reviewer can directly edit.
*   Notes Box: Text area for qualitative observations.

---

## 5. UI/UX Rules and Design Tokens

*   Primary color: #375f72 [calm slate blue] for active headers and highlights.
*   Font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif.
*   Shadows: box-shadow: 0 4px 12px rgba(16, 24, 40, 0.06) for modal and dropdown panels.
*   Transitions: Smooth 150ms opacity/background transitions for hovering cards and active toggles.
*   Scrollbars: Styled custom scrollbars for the individual sections using webkit pseudoselectors to keep scrollbars extremely thin [6px wide] and color-matched.

---

## 6. Acceptance Criteria

1.  Fully Hydrated Schema: Selecting a candidate loads and presents l0_context, l1_trigger, l2_when, l3_how, l4_safety blocks dynamically. Fallback values are shown if older formats are fetched.
2.  Toggle Lock and Logic Guard:
    *   If human_should_promote is checked [True], promote_level cannot be reject, and reject_reason is automatically set to null and disabled.
    *   If human_should_promote is unchecked [False], promote_level is locked to reject, and reject_reason field is required.
3.  Keyboard Efficiency:
    *   Ctrl + Enter [or Cmd + Enter] must trigger immediate save of the current card and fetch the API POST /internal/proactive-test/reviews.
    *   Upon a successful review submission, the next card in the active queue must be automatically highlighted and selected.
4.  Statistical Syncing:
    *   Any successful review action updates the global stats count [Total reviewed, Promote rate, Avg metrics] without needing page reload.
5.  No WeChat Outbound Execution: No actions on this page can bypass sandbox rules or perform production pushes to live users.
