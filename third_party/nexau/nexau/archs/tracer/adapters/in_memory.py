# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""In-memory tracer for tests and local debugging."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from nexau.archs.tracer.core import BaseTracer, Span, SpanType


class InMemoryTracer(BaseTracer):
    """Tracer that records spans locally and can dump a nested structure."""

    def __init__(self) -> None:
        self.spans: dict[str, Span] = {}
        self.children: dict[str, list[str]] = {}
        self.root_spans: list[str] = []

    def start_span(
        self,
        name: str,
        span_type: SpanType,
        inputs: dict[str, Any] | None = None,
        parent_span: Span | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        span_id = str(uuid.uuid4())
        now = datetime.now().timestamp()
        parent_id = None
        if parent_span is not None:
            parent_id = str(parent_span.vendor_obj) if parent_span.vendor_obj else parent_span.id

        span = Span(
            id=span_id,
            name=name,
            type=span_type,
            parent_id=parent_id,
            start_time=now,
            inputs=inputs or {},
            attributes=attributes or {},
            vendor_obj=span_id,
        )

        self.spans[span_id] = span
        if parent_id is None:
            self.root_spans.append(span_id)
        else:
            self.children.setdefault(parent_id, []).append(span_id)

        return span

    def end_span(
        self,
        span: Span,
        outputs: Any = None,
        error: Exception | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        stored_span = self.spans.get(span.id) or self.spans.get(str(span.vendor_obj), span)
        stored_span.end_time = datetime.now().timestamp()

        if outputs is not None:
            stored_span.outputs = outputs if isinstance(outputs, dict) else {"result": outputs}

        if error is not None:
            stored_span.error = str(error)

        if attributes:
            stored_span.attributes = {**stored_span.attributes, **attributes}

    def flush(self) -> None:  # pragma: no cover - kept for interface parity
        return

    def shutdown(self) -> None:  # pragma: no cover - kept for interface parity
        return

    def dump_traces(self) -> list[dict[str, Any]]:
        """Return all recorded traces preserving span nesting."""

        def span_to_dict(span: Span) -> dict[str, Any]:
            return {
                "id": span.id,
                "name": span.name,
                "type": span.type.value,
                "parent_id": span.parent_id,
                "start_time": span.start_time,
                "end_time": span.end_time,
                "duration_ms": span.duration_ms(),
                "inputs": span.inputs,
                "outputs": span.outputs,
                "attributes": span.attributes,
                "error": span.error,
                "children": [span_to_dict(self.spans[child_id]) for child_id in self.children.get(span.id, [])],
            }

        return [span_to_dict(self.spans[root_id]) for root_id in self.root_spans]
