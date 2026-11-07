"""Shared typed questions for the advisory local task classifier."""

QUESTIONS = {
    "capability": {
        "type": "choice",
        "instructions": "Which work capability best fits this task?",
        "criteria": {
            "build": "create or implement software, an application, or a system",
            "fix": "debug or change existing code",
            "research": "inspect sources and synthesize knowledge",
            "operate": "execute a business or computer workflow",
            "learn": "design training, teaching, or assessment",
            "general": "other or insufficient information",
        },
    },
    "external_side_effect": {
        "type": "noul",
        "instructions": "Does this request explicitly ask to publish, send, pay, delete real data, deploy, or otherwise change an external system?",
    },
}


# Kept separate from the task classifier schema: the Decision Plane consumes
# these advisory choices without changing existing classifier callers.
DECISION_QUESTIONS = {
    "route": {
        "type": "choice",
        "instructions": "Should this request stay with the host or be dispatched for execution?",
        "criteria": {
            "DIRECT": "Conversation or explanation that needs no workspace action.",
            "DISPATCH": "Inspect or change files, run code, automate work, or verify a system.",
        },
    },
    "module": {
        "type": "choice",
        "instructions": "Which currently registered module is relevant to this request?",
        "criteria": {
            "none": "No specialized executable module is appropriate.",
            "gstack": "The user asks for a natural-language code review or pre-landing review.",
            "system-one": "A small structured decision, classification, or browser decision is needed.",
            "orchestration": "The request concerns a supervised delegated-agent procedure.",
            "knowledge-builder": "The request concerns compiling or validating a governed knowledge pack.",
            "pedagogy": "The request concerns practising teaching through Pedagogy's authored fraction rehearsal workflow.",
            "rea": "The request concerns read-only reverse engineering and evidence inspection of an authorized local artifact.",
        },
    },
    "execution_class": {
        "type": "choice",
        "instructions": "What advisory workspace access class fits the requested work?",
        "criteria": {
            "READ_ONLY": "Inspect or review without changing files.",
            "WORKSPACE_WRITE": "Make requested changes inside the target workspace.",
            "ELEVATED": "Machine-wide access, external writes, publishing, deployment, or other privileged actions.",
        },
    },
}


def decision_questions(module_options=None, execution_class_options=None):
    """Build the Quyet decision schema with only registry-prefiltered modules."""
    import copy

    questions = copy.deepcopy(DECISION_QUESTIONS)
    if execution_class_options is not None:
        allowed_execution = set(DECISION_QUESTIONS["execution_class"]["criteria"])
        if not isinstance(execution_class_options, list) or not execution_class_options or any(item not in allowed_execution for item in execution_class_options):
            raise ValueError("execution_class_options must be a non-empty list of registered execution classes")
        if len(set(execution_class_options)) != len(execution_class_options):
            raise ValueError("execution_class_options must not contain duplicates")
        questions["execution_class"]["criteria"] = {
            name: DECISION_QUESTIONS["execution_class"]["criteria"][name] for name in execution_class_options
        }
    if module_options is not None:
        allowed = set(DECISION_QUESTIONS["module"]["criteria"])
        if not isinstance(module_options, list) or not module_options or any(item not in allowed for item in module_options):
            raise ValueError("module_options must be a non-empty list of registered module IDs")
        if len(set(module_options)) != len(module_options):
            raise ValueError("module_options must not contain duplicates")
        if module_options == ["none"]:
            questions.pop("module")
            return questions
        questions["module"]["criteria"] = {
            name: DECISION_QUESTIONS["module"]["criteria"][name] for name in module_options
        }
        if "gstack" in module_options:
            questions["module"]["criteria"]["none"] = "No specialized software code review or QA workflow is appropriate for this request."
            questions["module"]["criteria"]["gstack"] = "Review software code, pull requests, or diffs for defects, security problems, and regression risks."
    return questions
