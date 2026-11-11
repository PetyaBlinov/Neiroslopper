"""Assistant document state; no windows, network or machine commands."""
from copy import deepcopy

from editor_history import EditHistory
from editor_tools.planner import Plan, transform_paths
from editor_tools.preview import snapshot
from editor_tools.scene import from_plan
from geometry import Sheet


class SandboxDocument:
    """Independent sketch, retaining the same editable tasks as the main editor."""
    def __init__(self, width=210, height=297):
        self.sheet = Sheet(width, height)
        self.paths = []
        self.task = self.anchor = None
        self.name = "Пробный лист"
        self.version = 0
        self.history = EditHistory()

    def snapshot(self):
        return snapshot({"sandbox": True, "connected": False, "simulation": False,
                         "sheet_width": self.sheet.width, "sheet_height": self.sheet.height,
                         "file_name": self.name, "busy": False, "feed_mm_min": 3000,
                         "preview_task": deepcopy(self.task), "preview_anchor": self.anchor},
                        self.paths, ("assistant-sandbox", self.version))

    def content(self):
        return self.paths, self.name, self.task, self.anchor

    def commit(self, before, plan: Plan):
        from assistant.draft_context import applied_task, plan_anchor
        if before.revision != self.snapshot().revision:
            raise ValueError("Пробный лист изменился. Подготовьте предложение заново.")
        # Complete validation precedes both history and document mutation.
        paths = transform_paths(before.paths, plan, self.sheet.width, self.sheet.height)
        task = applied_task(plan, self.task)
        anchor = plan_anchor(plan, self.anchor)
        self.history.push(self.content())
        self.paths, self.task, self.anchor = paths, task, anchor
        self.name = "Пробный лист · " + plan.explanation[:160]
        self.version += 1
        return self.snapshot()

    def undo(self):
        self.paths, self.name, self.task, self.anchor = self.history.undo(self.content())
        self.version += 1
        return self.snapshot()

    def redo(self):
        self.paths, self.name, self.task, self.anchor = self.history.redo(self.content())
        self.version += 1
        return self.snapshot()


def scene_for(before, proposal=None):
    """Derive object metadata only from checked plans and their actual paths."""
    from assistant.draft_context import applied_task
    task, paths = before.state.get("preview_task"), before.paths
    if proposal is not None and proposal.before.revision == before.revision:
        task = applied_task(proposal.reply.plan, task)
        paths = proposal.paths
    if not paths:
        return {"version": 1, "objects": [], "route": None}
    return from_plan(task, paths, before.state["sheet_width"], before.state["sheet_height"])


def template_for_prompt(templates, prompt, previous=None):
    """Choose an actual saved template; names are literal, never generated paths."""
    import re
    if templates is None:
        return None
    for name in templates.names():
        if re.fullmatch(r'(?:шаблон\s+)?[«"]?' + re.escape(name) + r'[»"]?(?:\s+для\s+.+)?', prompt.strip(), re.I):
            return templates.load(name)
    if previous and re.search(r'такую\s+же\s+(?:открытку|композицию|надпись|шаблон)', prompt, re.I):
        return templates.load(previous)
    return None


def selected_request(prompt, state):
    """Resolve an object pronoun only against a real, explicitly selected object."""
    import re
    selected = state.get("studio_selected")
    scene = state.get("studio_scene")
    if selected is None or not isinstance(scene, dict) or selected not in {item["id"] for item in scene.get("objects", [])}:
        return prompt
    match = re.fullmatch(r"\s*(сдвинь|перенеси|перемести|поверни|увеличь|уменьши|отрази)\s+"
                         r"(?:его|её|ее|это)(\s+.*)?", prompt, re.I | re.S)
    if match is None:
        return prompt
    action = "Сдвинь" if match[1].casefold() == "перемести" else match[1]
    return action + (match[2] or "")
