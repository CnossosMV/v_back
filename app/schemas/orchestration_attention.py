"""Shared attention and episode-entry contract for outbound automations."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class AttentionActivationApproval(BaseModel):
    impact_fingerprint: str = Field(..., min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    acknowledged: bool = True


class AttentionPolicyInput(BaseModel):
    """Authored semantics; workers must not infer these from execution order."""

    version: Literal[1] = 1
    attention_scope: str = Field(
        "contact.promotional",
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    ordinal: int = Field(0, ge=-1000, le=1000)
    ordinal_reason: str = Field(..., min_length=3, max_length=2000)
    entry_effect: Literal["occlude", "suspend", "exit", "reject_entry", "coexist"]
    exclusive_group: str | None = Field(
        None,
        min_length=1,
        max_length=120,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    target_purpose_keys: list[str] = Field(default_factory=list, max_length=100)
    target_exclusive_groups: list[str] = Field(default_factory=list, max_length=100)
    allowed_incoming_effects: list[
        Literal["occlude", "suspend", "exit", "coexist"]
    ] = Field(default_factory=lambda: ["occlude", "coexist"], max_length=4)
    allow_start_occluded: bool = True
    tie_policy: Literal["require_order", "perishability", "bounded_learning"] = "require_order"
    occluded_clock: Literal["wall_clock", "active_attention"] = "wall_clock"
    missed_window: Literal["expire", "grace", "next_occurrence"] = "expire"
    future_reservation: bool = False

    @model_validator(mode="after")
    def validate_effect_targets(self):
        self.target_purpose_keys = sorted(set(self.target_purpose_keys))
        self.target_exclusive_groups = sorted(set(self.target_exclusive_groups))
        self.allowed_incoming_effects = sorted(set(self.allowed_incoming_effects))
        targeted = bool(
            self.exclusive_group
            or self.target_purpose_keys
            or self.target_exclusive_groups
        )
        if self.entry_effect in {"suspend", "exit", "reject_entry"} and not targeted:
            raise ValueError(
                f"entry_effect={self.entry_effect} requires an exclusive_group or explicit targets"
            )
        if self.entry_effect == "coexist" and (
            self.target_purpose_keys or self.target_exclusive_groups
        ):
            raise ValueError("entry_effect=coexist cannot declare incompatible targets")
        return self
