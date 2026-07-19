# -*- coding: utf-8 -*-
"""Core package for the AI Agent Collaboration Workspace.

Modules:
- generic_workflow: agents, tasks, dependencies, context, and content packages;
- generic_engine: task execution, manager reviews, image output, and delivery;
- file_extractor: text and table extraction from uploaded files;
- llm_service: model providers, configuration, and multimodal requests;
- retry: network and API retry/recovery behavior.
"""

__all__ = ["generic_workflow", "generic_engine", "file_extractor", "llm_service", "retry"]
