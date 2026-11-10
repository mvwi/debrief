"use strict";

// How the analysis was done, plus the few metrics it invented. {placeholders} are filled
// from the data's meta block, so the numbers match whatever run was exported. A line marked
// [github] shows only when the export has GitHub data.

const METHOD = {
  question: "Where do you have to step in and steer your coding agents, and what would stop that? The session logs already hold the answer. Here's how debrief gets it out.",
  steps: [
    {
      id: "collect", title: "Collect the sessions", short: "Collect", glance: ["{sessions}", "sessions"],
      lede: "Read the agent sessions into one local database.",
      text: [
        "- {sessions} sessions that started between {from} and {to}.\n- [github] Each session is linked to its pull requests on GitHub: by its commits, or by the PR links and pushed branches in its own log. CI runs and review comments trace back to the session that wrote the code.",
      ],
      facts: [["{messages}", "messages"], ["{tool_calls}", "tool calls"], ["{prs}", "PRs"], ["{checks}", "CI runs"], ["{review_comments}", "review comments"]],
    },
    {
      id: "normalize", title: "Put every harness in one shape", short: "Normalize", glance: ["{human_turns}", "human messages"],
      lede: "Turn each harness's log format into one, and keep only what people typed.",
      text: [
        "- Each harness logs differently. All become one table of messages and one of tool calls.\n- Slash-command expansions, skill text and hook output arrive looking like user messages. They're set aside; counted as a person, they'd inflate every rate.",
      ],
    },
    {
      id: "label", title: "Label every message", short: "Label", glance: ["{labels}", "labels"],
      lede: "A small model answers the same questions about every human message: Claude Haiku by default, or [Jev](https://typesafe.ai) with --llm openrouter.",
      text: [
        "It sees the message plus everything the agent did since the person's last one, and answers:",
        "1. **Intent:** What was the message doing?\n2. **Correction:** Did it correct the agent, and how?\n3. **Pointer:** What did it point the agent to?\n4. **Reversal:** Did it reject a design the agent had already built?\n5. **Bug:** Did it catch a bug in the agent's work?\n6. **Overlooked:** Did it tell the agent something it could have found itself?\n7. **Left unfiled:** Had the agent flagged a problem and left it?",
        "Also labeled:",
        "- **Each session:** task type, how it opened, how specific the opening was, whether the agent planned first, whether tests changed, outcome.\n- [github] **Each failed CI run:** why it failed.\n- [github] **Each review comment:** what it was about, and whether it held up.",
        "Labels below {min_conf} confidence are dropped.",
      ],
    },
    {
      id: "find", title: "Turn labels into findings", short: "Find", glance: ["{findings}", "findings"],
      lede: "Claude Opus reads the labels from several angles and writes findings, each citing real examples.",
      text: [
        "The angles:",
        "- Code areas that draw corrections\n- Knowledge the agent was missing\n- Bugs found and left unfiled in the session\n- Correction patterns\n- Prompt habits\n- [github] CI failures\n- [github] Review comments",
        "The rules:",
        "- Each angle gets counts plus real examples, at most five from any one session.\n- Every finding must cite its examples by ID. Citations are checked, and a finding left with none is dropped.",
      ],
    },
    {
      id: "recommend", title: "Merge findings into ideas", short: "Ideas", glance: ["{recommendations}", "ideas"],
      lede: "Combine findings that describe the same problem into one idea for a change.",
      text: [
        "- One problem often shows up from several angles. A stale code comment can be a hotspot, a correction pattern and a knowledge gap at once.\n- Each idea gets a type, such as agent instructions, tests or a bug to file.\n- Reach, meaning sessions and people touched, is counted from the evidence, not taken from the model. Every list sorts by it.\n- A second, cheaper model pass checks every cited item against its idea. Items that don't support it are dropped, and reach counts only what's left.",
      ],
    },
  ],
};

// Metrics this analysis defines. `page: true` entries appear on the How it works page;
// the rest only supply ⓘ tooltips and link to the step that explains them (`anchor`).
const DEFS = [
  {
    id: "correction_rate", term: "Agent corrected (%)", page: true, better: "lower",
    short: "Share of human messages that corrected the agent's work: said it was wrong, had it redone, or caught a bug. Labels count only at confidence {min_conf} or higher.",
    def: "Share of human messages that corrected the agent's work. Labels count only at confidence {min_conf} or higher. A message counts once, even if it does more than one of these:",
    list: [
      ["Said it was wrong", "Told the agent something it did or said was wrong."],
      ["Had it redone", "Rejected an approach the agent had already built. Fixing one detail doesn't count."],
      ["Caught a bug", "Pointed out a bug or unwanted output in the agent's work."],
    ],
  },
  {
    id: "overlooked_info", term: "Agent overlooked (%)", page: true, better: "lower",
    short: "Share of human messages that pointed the agent to information it already had access to but didn't find.",
    def: "Share of human messages that told the agent something it could have found itself: code, test results, git, PR or CI state, docs. The work isn't necessarily wrong; the agent just didn't look.",
    example: "“there are more findings”, sent right after the agent reported none open.",
  },
  {
    id: "found_not_filed", term: "Found, not filed", page: true,
    short: "The agent named a problem outside its task and left it. It wasn't filed during the session.",
    def: "The agent spotted a problem outside its task, such as a test failing on main, and left it. It wasn't filed during the session. Each one becomes a possible-bug idea.",
  },
  {
    id: "shipped_rate", term: "Shipped rate", page: true, better: "higher",
    short: "Share of sessions that ended with work committed or a PR ready.",
    def: "Share of sessions that ended with work committed or a PR ready. Judged from how the transcript ends, not whether the PR later merged.",
  },
  {
    id: "mistake_types", term: "What the agent got wrong", page: true,
    short: "For messages that said the agent was wrong, the kind of mistake.",
    def: "For messages that said the agent was wrong, the kind of mistake:",
    list: [
      ["Wrong assumption", "Assumed something false."],
      ["Missed requirement", "Left out something that was asked for."],
      ["Scope", "Did too much or too little."],
      ["Factual error", "Stated something untrue."],
      ["Skipped a step", "Didn't run tests, or didn't ask first."],
      ["Convention", "Broke a style or project convention."],
      ["Unclassified", "The model wasn't at least 50% sure."],
    ],
  },
  {
    id: "hotspot", term: "Where corrections happen", page: true,
    short: "Directories the agent wrote to just before a person corrected it.",
    def: "Directories the agent wrote to just before a person corrected it. One session in a directory usually means one hard task, not a weak spot.",
  },
  // Tooltip-only entries.
  { id: "by_model", anchor: "collect", short: "Grouped by model, across harnesses. 1M-context variants count with their base model. Models with fewer than 10 sessions are left out." },
  { id: "sessions", anchor: "collect", short: "The agent sessions in this report: {sessions}, started between {from} and {to}." },
  { id: "settled_pr", anchor: "collect", short: "Only PRs that merged or closed count, so open PRs mid-iteration don't skew the numbers." },
  { id: "ci_causes", anchor: "label", short: "Why a check failed, judged from the tail of its log: infra, test regression, flaky test, build error or timeout. Passed on re-run is different: it's seen, not judged, when the same check later passed on the same commit." },
  { id: "ci_where", anchor: "label", short: "Where each failed run broke, read from its log: the failing Go package, test and file. The log names the test that failed, not the change that caused it." },
  { id: "review_where", anchor: "collect", short: "Inline review comments by the file type and directory they point at. The table shows directories with at least 5 comments, sorted by how often reviewers were wrong." },
  { id: "held_up", anchor: "label", short: "Whether a review comment turned out right, judged from its thread: valid and fixed, valid and open, outdated, wrong, or unclear." },
  { id: "turn_kinds", anchor: "label", short: "What each human message was doing: a question, a new request, an approval, a correction, an answer, or a status check. Counts every label at any confidence, so Correction here runs higher than Agent corrected, which counts labels at {min_conf} or higher." },
  { id: "prompt_styles", anchor: "label", short: "How the person opened the session. Compared only on sessions with a merged or closed PR, so the counts are smaller than elsewhere on this page; small groups." },
  { id: "skills_use", anchor: "normalize", short: "Typed: a person ran a /command. Auto-loaded: the agent loaded a skill itself." },
  { id: "reach", anchor: "recommend", short: "How many sessions and people an idea's evidence comes from. Lists are sorted by it." },
  { id: "per_person", anchor: "collect", short: "Usage only. Correction rates aren't broken out per person." },
];
