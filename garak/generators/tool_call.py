# SPDX-FileCopyrightText: Portions Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""OpenAI-compatible generator with tool-call capture.

Subclasses ``OpenAICompatible`` to pass tool definitions to the LLM and
capture structured tool calls from the response. Works with any
OpenAI-compatible endpoint (OpenAI, Ollama ``/v1``, Groq, Azure, etc.).

Tool calls are stored in ``Message.notes["tool_calls"]`` as a list of
``{"function": str, "arguments": dict}`` dicts so that detectors can
inspect them without modifying core garak data structures.
"""

import inspect
import json
import logging
from typing import List, Union

import backoff
import openai

import garak.exception
from garak import _config
from garak.attempt import Conversation, Message
from garak.generators.openai import OpenAICompatible


class ToolCallGenerator(OpenAICompatible):
    """Generator that captures tool calls via any OpenAI-compatible API.

    Set ``uri`` (or ``api_base``) to point at your provider::

        # Ollama
        gen = ToolCallGenerator(name="qwen2.5-coder:14b")
        gen.uri = "http://localhost:11434/v1"

        # OpenAI (default)
        gen = ToolCallGenerator(name="gpt-4o")

    Pass tool schemas via ``tool_schemas`` (OpenAI function-calling format).
    """

    active = True
    generator_family_name = "ToolCall"
    ENV_VAR = "OPENAI_API_KEY"

    DEFAULT_PARAMS = OpenAICompatible.DEFAULT_PARAMS | {
        "tool_schemas": None,
    }

    def __init__(self, name="", config_root=_config, uri=None, api_key=None):
        self._init_uri = uri
        self._init_api_key = api_key
        # Pre-set credentials so parent __init__ can connect
        if api_key is not None:
            self.api_key = api_key
        if uri is not None:
            self.uri = uri
        super().__init__(name, config_root=config_root)
        # Re-apply overrides in case parent init reset them
        if uri is not None:
            self.uri = uri
        if api_key is not None:
            self.api_key = api_key
            self._load_unsafe()

    def _validate_config(self):
        """Allow custom endpoints (e.g. Ollama) without OPENAI_API_KEY."""
        if self._init_uri is not None or self._init_api_key is not None:
            return
        super()._validate_config()

    @backoff.on_exception(
        backoff.fibo,
        (
            openai.RateLimitError,
            openai.InternalServerError,
            openai.APITimeoutError,
            openai.APIConnectionError,
            garak.exception.GeneratorBackoffTrigger,
        ),
        max_value=70,
    )
    def _call_model(
        self, prompt: Conversation, generations_this_call: int = 1
    ) -> List[Union[Message, None]]:
        if self.client is None:
            self._load_unsafe()

        generator = self.generator

        create_args = {}
        if self.supports_multiple_generations:
            create_args["n"] = generations_this_call
        elif "n" not in self.suppressed_params:
            create_args["n"] = 1

        for arg in inspect.signature(generator.create).parameters:
            if arg == "model":
                create_args[arg] = self.name
                continue
            if arg == "extra_params":
                continue
            if hasattr(self, arg) and arg not in self.suppressed_params:
                if getattr(self, arg) is not None:
                    create_args[arg] = getattr(self, arg)

        if hasattr(self, "extra_params"):
            for k, v in self.extra_params.items():
                create_args[k] = v

        # Add tool schemas
        if self.tool_schemas:
            create_args["tools"] = self.tool_schemas

        if isinstance(prompt, Conversation):
            messages = self._conversation_to_list(prompt)
        elif isinstance(prompt, list):
            messages = prompt
        else:
            logging.error(
                "Expected Conversation or list for %s, got %s",
                self.generator_family_name,
                type(prompt),
            )
            return []

        create_args["messages"] = messages

        try:
            response = generator.create(**create_args)
        except openai.BadRequestError as e:
            logging.exception(e)
            return [None]
        except json.decoder.JSONDecodeError as e:
            logging.exception(e)
            if self.retry_json:
                raise garak.exception.GeneratorBackoffTrigger from e
            raise

        if not hasattr(response, "choices") or not response.choices:
            logging.debug("No choices in response: %s", repr(response))
            if self.retry_json:
                raise garak.exception.GeneratorBackoffTrigger(
                    "no .choices in response"
                )
            return [None]

        result = []
        for choice in response.choices:
            msg_data = choice.message
            text = msg_data.content
            message = Message(text=text)

            # Capture tool calls
            if msg_data.tool_calls:
                tool_calls = []
                for tc in msg_data.tool_calls:
                    args = tc.function.arguments
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {"_raw": args}
                    tool_calls.append(
                        {
                            "function": tc.function.name,
                            "arguments": args,
                        }
                    )
                message.notes["tool_calls"] = tool_calls

            result.append(message)

        if len(result) != generations_this_call:
            raise garak.exception.BadGeneratorException(
                "Generator returned %d responses, expected %d"
                % (len(result), generations_this_call)
            )

        return result
