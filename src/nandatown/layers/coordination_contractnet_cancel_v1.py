"""Contract-Net with issuer-authorized, terminal task cancellation."""

from nandatown.layers import register
from nandatown.layers.coordination import ContractNet


@register("coordination", "contractnet.cancel.v1")
class CancellableContractNet(ContractNet):
    """Contract-Net whose issuer may terminally cancel an open task."""

    def announce(self, issuer: str, task_id: str, spec: dict,
                 rule: str = "lowest") -> bool | None:
        existing = self.tasks.get(task_id)
        if existing is not None and existing["state"] == "cancelled":
            self.engine.emit(
                "town", "task_announce_rejected", task_id,
                {"issuer": issuer, "original_issuer": existing["issuer"],
                 "reason": "task cancelled"})
            return False
        return super().announce(issuer, task_id, spec, rule)

    def cancel(self, task_id: str, actor: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            self.engine.emit("town", "task_cancel_rejected", task_id,
                             {"actor": actor, "reason": "unknown task"})
            return False
        if actor != task["issuer"]:
            self.engine.emit(
                "town", "task_cancel_rejected", task_id,
                {"actor": actor, "issuer": task["issuer"],
                 "reason": "not issuer"})
            return False
        if task["state"] != "open":
            reason = ("task cancelled" if task["state"] == "cancelled"
                      else "task closed")
            self.engine.emit("town", "task_cancel_rejected", task_id,
                             {"actor": actor, "reason": reason})
            return False
        task["state"] = "cancelled"
        self.engine.emit(actor, "task_cancelled", task_id,
                         {"issuer": actor})
        return True

    def bid(self, task_id: str, bidder: str, cents: int) -> bool:
        task = self.tasks[task_id]
        if task["state"] == "cancelled":
            self.engine.emit("town", "bid_rejected", task_id,
                             {"bidder": bidder, "cents": cents,
                              "reason": "task cancelled"})
            return False
        return super().bid(task_id, bidder, cents)

    def award(self, task_id: str) -> tuple[str, int] | None:
        task = self.tasks[task_id]
        if task["state"] == "cancelled":
            self.engine.emit("town", "award_rejected", task_id,
                             {"issuer": task["issuer"],
                              "reason": "task cancelled"})
            return None
        return super().award(task_id)


@register("coordination", "contractnet.cancel.nonterminal.v1")
class NonterminalCancellationControl(CancellableContractNet):
    """Negative control: records cancellation but leaves the task open."""

    def cancel(self, task_id: str, actor: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None or actor != task["issuer"] or task["state"] != "open":
            return super().cancel(task_id, actor)
        self.engine.emit(actor, "task_cancelled", task_id,
                         {"issuer": actor})
        return True
