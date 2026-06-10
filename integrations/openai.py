"""
Unified OpenAI API interface.

Provides centralized client management, Responses API wrapper, and usage logging.
Benefits: 40-80% better cache utilization, lower latency, simpler interface.

Key functions:
- get_openai_client(): Get configured OpenAI client singleton
- complete(): Generate completion using Responses API
- complete_with_usage(): Generate completion with usage stats
- analyze_image(): Analyze image using Vision capabilities
- log_*_usage(): Usage logging for different API operations
"""

import base64
import os
import threading
from datetime import datetime

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError

from config import get_logger, supports_reasoning
from storage.settings import get_configured_openai_model

logger = get_logger("ai")

# =============================================================================
# Client Management
# =============================================================================

# Singleton client instance with thread lock
_client = None
_client_lock = threading.Lock()


def get_openai_client():
    """
    Get configured OpenAI client singleton (thread-safe).

    Uses OPENAI_API_KEY from environment and supports dynamic model
    configuration via config.py.

    Returns:
        OpenAI: Configured client instance

    Raises:
        ValueError: If OPENAI_API_KEY environment variable is not set
    """
    global _client

    # Double-checked locking pattern for thread safety
    if _client is None:
        with _client_lock:
            if _client is None:
                api_key = os.getenv("OPENAI_API_KEY")
                if not api_key:
                    logger.error("OPENAI_ERROR: OPENAI_API_KEY not found in environment")
                    raise ValueError("OPENAI_API_KEY environment variable not set")

                _client = OpenAI(api_key=api_key)
                logger.info("OPENAI: Client initialized successfully")

    return _client


# =============================================================================
# Responses API Wrapper
# =============================================================================


def _log_and_record_usage(response, context):
    """Log a response's token usage and add it to the daily usage tracker."""
    if not (hasattr(response, "usage") and response.usage):
        return

    usage = response.usage
    logger.info(
        f"AI_{context}_USAGE: "
        f"input={getattr(usage, 'input_tokens', 'N/A')}, "
        f"output={getattr(usage, 'output_tokens', 'N/A')}, "
        f"total={getattr(usage, 'total_tokens', 'N/A')}"
    )
    try:
        from storage.ai_usage import record_usage

        record_usage(
            context,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
    except Exception:
        pass  # accounting must never break generation


def _build_api_params(
    messages,
    input_text,
    instructions,
    model,
    max_tokens,
    temperature,
    reasoning_effort,
    tools=None,
):
    """Build Responses API parameters from Chat Completions-style inputs."""
    params = {"model": model}
    if tools:
        params["tools"] = tools

    if messages:
        system_content = None
        user_messages = []

        for msg in messages:
            if msg.get("role") in ("system", "developer"):
                system_content = msg.get("content", "")
            else:
                user_messages.append(msg)

        if instructions:
            params["instructions"] = instructions
        elif system_content:
            params["instructions"] = system_content

        if len(user_messages) == 1:
            params["input"] = user_messages[0].get("content", "")
        elif user_messages:
            params["input"] = user_messages
        else:
            params["input"] = system_content or ""

    elif input_text:
        params["input"] = input_text
        if instructions:
            params["instructions"] = instructions
    else:
        raise ValueError("Either 'messages' or 'input_text' must be provided")

    if max_tokens:
        params["max_output_tokens"] = max_tokens
    if reasoning_effort is not None and supports_reasoning(model):
        params["reasoning"] = {"effort": reasoning_effort}
    if temperature is not None and not supports_reasoning(model):
        params["temperature"] = temperature

    return params


def complete(
    messages=None,
    input_text=None,
    instructions=None,
    model=None,
    max_tokens=None,
    temperature=None,
    context=None,
    reasoning_effort=None,
):
    """
    Generate a completion using OpenAI's Responses API.

    Args:
        messages: List of message dicts with role/content (Chat Completions format)
                  Will be converted to Responses API format automatically.
        input_text: Direct text input (alternative to messages)
        instructions: System instructions (extracted from messages if not provided)
        model: Model to use (defaults to configured model)
        max_tokens: Maximum tokens in response
        temperature: Sampling temperature
        context: Optional context string for logging (e.g., "BIRTHDAY_MESSAGE")
        reasoning_effort: Reasoning effort for GPT-5+ ("low", "medium", "high", etc.)

    Returns:
        str: The generated text response

    Raises:
        Exception: If API call fails
    """
    client = get_openai_client()
    model = model or get_configured_openai_model()
    context = context or "COMPLETION"

    params = _build_api_params(
        messages, input_text, instructions, model, max_tokens, temperature, reasoning_effort
    )

    logger.info(f"AI_{context}: Calling Responses API with model={model}")

    try:
        response = client.responses.create(**params)

        _log_and_record_usage(response, context)

        text = response.output_text or ""
        if not text:
            logger.warning(
                f"AI_{context}: Empty output_text — likely reasoning consumed entire budget"
            )
        return text

    except RateLimitError as e:
        logger.error(f"AI_{context}_ERROR: Rate limit exceeded: {e}")
        raise
    except APITimeoutError as e:
        logger.error(f"AI_{context}_ERROR: API request timed out: {e}")
        raise
    except APIConnectionError as e:
        logger.error(f"AI_{context}_ERROR: Connection failed: {e}")
        raise
    except APIError as e:
        logger.error(f"AI_{context}_ERROR: API error: {e}")
        raise


def complete_raw(
    input=None,
    instructions=None,
    model=None,
    max_tokens=None,
    temperature=None,
    context=None,
    reasoning_effort=None,
    tools=None,
):
    """
    Call the Responses API and return the raw response object.

    For tool-calling loops that need to inspect response.output for
    function_call items. `input` may be a string or a list of Responses API
    input items (messages, function_call_output items, prior output items).
    Same exception contract as complete(): API errors propagate.

    Returns:
        Response: The raw OpenAI Responses API response object
    """
    client = get_openai_client()
    model = model or get_configured_openai_model()
    context = context or "COMPLETION"

    params = _build_api_params(
        None, input, instructions, model, max_tokens, temperature, reasoning_effort, tools=tools
    )

    logger.info(
        f"AI_{context}: Calling Responses API with model={model}"
        f"{' (tools enabled)' if tools else ''}"
    )

    try:
        response = client.responses.create(**params)
        _log_and_record_usage(response, context)
        return response

    except RateLimitError as e:
        logger.error(f"AI_{context}_ERROR: Rate limit exceeded: {e}")
        raise
    except APITimeoutError as e:
        logger.error(f"AI_{context}_ERROR: API request timed out: {e}")
        raise
    except APIConnectionError as e:
        logger.error(f"AI_{context}_ERROR: Connection failed: {e}")
        raise
    except APIError as e:
        logger.error(f"AI_{context}_ERROR: API error: {e}")
        raise


def complete_with_usage(
    messages=None,
    input_text=None,
    instructions=None,
    model=None,
    max_tokens=None,
    temperature=None,
    context=None,
    reasoning_effort=None,
):
    """
    Generate a completion and return both text and usage info.

    Same exception contract as complete(): API errors propagate to the caller.
    On a successful call, returns ("", usage_dict) when output_text is empty
    (e.g. reasoning models that exhausted the budget) — never None.

    Returns:
        tuple: (response_text, usage_dict) where usage_dict contains token counts
    """
    client = get_openai_client()
    model = model or get_configured_openai_model()
    context = context or "COMPLETION"

    params = _build_api_params(
        messages, input_text, instructions, model, max_tokens, temperature, reasoning_effort
    )

    logger.info(f"AI_{context}: Calling Responses API with model={model}")

    response = client.responses.create(**params)

    usage_dict = {}
    if hasattr(response, "usage") and response.usage:
        usage = response.usage
        usage_dict = {
            "input_tokens": getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "output_tokens", 0),
            "total_tokens": getattr(usage, "total_tokens", 0),
        }
    _log_and_record_usage(response, context)

    text = response.output_text or ""
    if not text:
        logger.warning(f"AI_{context}: Empty output_text — likely reasoning consumed entire budget")
    return text, usage_dict


# =============================================================================
# Vision API Wrapper
# =============================================================================


def analyze_image(
    image_path: str,
    prompt: str,
    max_tokens: int = 100,
    context: str = "IMAGE_ANALYSIS",
) -> str | None:
    """
    Analyze an image using Responses API with vision capabilities.

    Centralized in integrations/openai.py following existing patterns.
    Uses the same model as text completions (supports vision).

    Args:
        image_path: Path to the image file to analyze
        prompt: Text prompt describing what to analyze/extract
        max_tokens: Maximum tokens in response (default: 100)
        context: Optional context string for logging

    Returns:
        str: The analysis result text, or None if analysis fails
    """
    client = get_openai_client()
    model = get_configured_openai_model()

    try:
        # Read and encode image to base64
        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        # Determine image type from extension
        ext = image_path.lower().split(".")[-1]
        mime_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
            ext, "image/png"
        )

        logger.info(f"AI_{context}: Calling Responses API with vision, model={model}")

        # Use Responses API with multimodal input
        # Format verified from OpenAI docs: input_image with base64 image_url
        response = client.responses.create(
            model=model,
            input=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {
                            "type": "input_image",
                            "image_url": f"data:{mime_type};base64,{image_data}",
                            "detail": "low",  # 512x512, 85 tokens - efficient for analysis
                        },
                    ],
                }
            ],
            max_output_tokens=max_tokens,
        )

        _log_and_record_usage(response, context)

        return response.output_text

    except FileNotFoundError:
        logger.error(f"AI_{context}_ERROR: Image file not found: {image_path}")
        return None
    except RateLimitError as e:
        logger.error(f"AI_{context}_ERROR: Rate limit exceeded: {e}")
        return None
    except APITimeoutError as e:
        logger.error(f"AI_{context}_ERROR: API request timed out: {e}")
        return None
    except APIConnectionError as e:
        logger.error(f"AI_{context}_ERROR: Connection failed: {e}")
        return None
    except APIError as e:
        logger.error(f"AI_{context}_ERROR: API error: {e}")
        return None
    except Exception as e:
        logger.error(f"AI_{context}_ERROR: Unexpected error analyzing image: {e}")
        return None


# =============================================================================
# Usage Logging Functions
# =============================================================================


def log_chat_completion_usage(response, operation_name, logger):
    """
    Log token usage for chat completion API calls.

    Args:
        response: OpenAI chat completion response object
        operation_name: String describing the operation (e.g., "SINGLE_BIRTHDAY")
        logger: Logger instance to use for logging
    """
    try:
        usage = response.usage
        if usage:
            logger.info(
                f"{operation_name}_USAGE: Token usage - "
                f"prompt: {usage.prompt_tokens}, "
                f"completion: {usage.completion_tokens}, "
                f"total: {usage.total_tokens}"
            )
        else:
            logger.warning(f"{operation_name}_USAGE: No usage data available in response")
    except Exception as e:
        logger.error(f"{operation_name}_USAGE: Failed to log token usage: {e}")


def log_image_generation_usage(
    response,
    operation_name,
    logger,
    image_count=1,
    quality=None,
    image_size=None,
    model=None,
):
    """
    Log usage for image generation API calls with enhanced parameter tracking.

    Args:
        response: OpenAI image generation response object
        operation_name: String describing the operation (e.g., "IMAGE_GENERATION")
        logger: Logger instance to use for logging
        image_count: Number of images generated (default: 1)
        quality: Quality setting used ("low", "medium", "high", "auto")
        image_size: Size setting used (e.g., "1024x1024", "auto")
        model: Model used for image generation
    """
    try:
        if hasattr(response, "data") and response.data:
            images_generated = len(response.data)

            logger.info(
                f"{operation_name}_USAGE: Image generation completed - "
                f"images requested: {image_count}, "
                f"images generated: {images_generated}"
            )

            try:
                from storage.ai_usage import record_usage

                # Token counts don't apply to image calls; count the call itself
                record_usage(operation_name, 0, 0)
            except Exception:
                pass

            if hasattr(response, "created") and response.created:
                timestamp = datetime.fromtimestamp(response.created).strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"{operation_name}_USAGE: Generated at: {timestamp}")

            cost_params = []
            if quality:
                cost_params.append(f"quality: {quality}")
            if image_size:
                cost_params.append(f"size: {image_size}")
            if model:
                cost_params.append(f"model: {model}")

            if cost_params:
                logger.info(f"{operation_name}_USAGE: Parameters - {', '.join(cost_params)}")

            if hasattr(response.data[0], "model"):
                actual_model = response.data[0].model
                logger.info(f"{operation_name}_USAGE: Response model: {actual_model}")
        else:
            logger.warning(f"{operation_name}_USAGE: No image data available in response")
    except Exception as e:
        logger.error(f"{operation_name}_USAGE: Failed to log image generation usage: {e}")


def log_web_search_usage(response, operation_name, logger):
    """
    Log usage for web search API calls (responses.create).

    Args:
        response: OpenAI responses.create response object
        operation_name: String describing the operation (e.g., "WEB_SEARCH")
        logger: Logger instance to use for logging
    """
    try:
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            if hasattr(usage, "input_tokens") and hasattr(usage, "output_tokens"):
                total_tokens = (usage.input_tokens or 0) + (usage.output_tokens or 0)
                logger.info(
                    f"{operation_name}_USAGE: Token usage - "
                    f"input: {usage.input_tokens}, "
                    f"output: {usage.output_tokens}, "
                    f"total: {total_tokens}"
                )
            elif hasattr(usage, "prompt_tokens") and hasattr(usage, "completion_tokens"):
                logger.info(
                    f"{operation_name}_USAGE: Token usage - "
                    f"prompt: {usage.prompt_tokens}, "
                    f"completion: {usage.completion_tokens}, "
                    f"total: {usage.total_tokens}"
                )
            else:
                logger.info(
                    f"{operation_name}_USAGE: Usage object available but format unknown: {usage}"
                )

        if hasattr(response, "output_text"):
            output_length = len(response.output_text) if response.output_text else 0
            logger.info(
                f"{operation_name}_USAGE: Web search completed - "
                f"output length: {output_length} characters"
            )
        else:
            logger.warning(f"{operation_name}_USAGE: No output_text in web search response")
    except Exception as e:
        logger.error(f"{operation_name}_USAGE: Failed to log web search usage: {e}")


def log_generic_api_usage(response, operation_name, logger, additional_info=None):
    """
    Generic usage logging function that handles different response types.

    Args:
        response: Any OpenAI API response object
        operation_name: String describing the operation
        logger: Logger instance to use for logging
        additional_info: Optional dictionary with extra info to log
    """
    try:
        logged_something = False

        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            logger.info(
                f"{operation_name}_USAGE: Token usage - "
                f"prompt: {usage.prompt_tokens}, "
                f"completion: {usage.completion_tokens}, "
                f"total: {usage.total_tokens}"
            )
            logged_something = True

        elif hasattr(response, "data") and response.data:
            images_count = len(response.data)
            logger.info(f"{operation_name}_USAGE: Generated {images_count} image(s)")
            logged_something = True

        elif hasattr(response, "output_text"):
            output_length = len(response.output_text) if response.output_text else 0
            logger.info(f"{operation_name}_USAGE: Output length: {output_length} characters")
            logged_something = True

        if additional_info:
            for key, value in additional_info.items():
                logger.info(f"{operation_name}_USAGE: {key}: {value}")
                logged_something = True

        if not logged_something:
            logger.warning(f"{operation_name}_USAGE: No recognizable usage data in response")
    except Exception as e:
        logger.error(f"{operation_name}_USAGE: Failed to log API usage: {e}")
