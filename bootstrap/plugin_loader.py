"""Validate explicitly selected, trusted solution implementations at startup.

No discovery, import strings, downloads or artifact deserialization occur here.
Python code is trusted; metadata validation is not a security sandbox.
"""

import inspect
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from src.domain.kernels.models import KernelSpec
from src.domain.strategy import SolutionKernel

SOLUTION_KERNEL_CONTRACT = "solution-kernel/v1"
_NAME = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True, slots=True)
class PluginBinding:
    name: str
    implementation: SolutionKernel
    contract_version: str = SOLUTION_KERNEL_CONTRACT


def load_approved_plugins(bindings: Iterable[PluginBinding]) -> Mapping[str, SolutionKernel]:
    """Validate code-selected bindings and reject duplicates/incompatible contracts.

    Approval means the composition root explicitly selected the object; no code
    signature, model artifact, output semantics or external approval is verified.
    """
    registered: dict[str, SolutionKernel] = {}
    for binding in bindings:
        if _NAME.fullmatch(binding.name) is None:
            raise ValueError("Plugin names must use module_name.local_name")
        if binding.name in registered:
            raise ValueError(f"Duplicate plugin: {binding.name}")
        if binding.contract_version != SOLUTION_KERNEL_CONTRACT:
            raise ValueError(f"Incompatible plugin contract: {binding.name}")
        kernel = binding.implementation
        if not isinstance(kernel.spec, KernelSpec):
            raise TypeError(f"Plugin must expose KernelSpec: {binding.name}")
        if _VERSION.fullmatch(kernel.spec.version) is None:
            raise ValueError(f"Plugin requires an explicit valid version: {binding.name}")
        if kernel.spec.kind not in {"ai", "ml", "rule", "custom"}:
            raise ValueError(f"Invalid plugin kind: {binding.name}")
        execute = getattr(kernel, "execute", None)
        if not callable(execute):
            raise TypeError(f"Plugin execute must be callable: {binding.name}")
        if not (
            inspect.iscoroutinefunction(execute) or inspect.iscoroutinefunction(execute.__call__)
        ):
            raise TypeError(f"Plugin execute must be async: {binding.name}")
        registered[binding.name] = kernel
    return MappingProxyType(registered)
