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

"""Tracer module for agent tracing and observability."""

from nexau.archs.tracer.composite import CompositeTracer
from nexau.archs.tracer.context import TraceContext, get_current_span, reset_current_span, set_current_span
from nexau.archs.tracer.core import BaseTracer, Span, SpanType

__all__ = [
    "BaseTracer",
    "Span",
    "SpanType",
    "TraceContext",
    "CompositeTracer",
    "get_current_span",
    "set_current_span",
    "reset_current_span",
]
