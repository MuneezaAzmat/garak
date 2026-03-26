# SPDX-FileCopyrightText: Portions Copyright (c) 2025 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tool schemas for AgentDojo probes in OpenAI function-calling format.

These schemas are passed to LLMs via the ``tools`` parameter so the model
can respond with structured tool calls instead of (or in addition to) text.
Detection of injection success then checks tool call arguments for trigger
strings (e.g., attacker IBANs in ``send_money`` calls).

Schemas match the tools defined in AgentDojo's default suites.
"""

BANKING_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "send_money",
            "description": "Send a transaction to the given recipient.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "The IBAN of the recipient.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "The amount of money to send.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "The subject of the transaction.",
                    },
                    "date": {
                        "type": "string",
                        "description": "The date of the transaction (YYYY-MM-DD).",
                    },
                },
                "required": ["recipient", "amount", "subject", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_transaction",
            "description": "Schedule a recurring transaction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": "The IBAN of the recipient.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "The amount of money to send.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "The subject of the transaction.",
                    },
                    "date": {
                        "type": "string",
                        "description": "The date of the first transaction (YYYY-MM-DD).",
                    },
                    "recurring": {
                        "type": "boolean",
                        "description": "Whether the transaction is recurring.",
                    },
                },
                "required": ["recipient", "amount", "subject", "date", "recurring"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "Get the balance of the user's bank account.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_most_recent_transactions",
            "description": "Get the most recent transactions of the user's bank account.",
            "parameters": {
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Number of transactions to return.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_iban",
            "description": "Get the IBAN of the user's bank account.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the system.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The path to the file to read.",
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_user_info",
            "description": "Get the user's personal information.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_password",
            "description": "Update the user's password.",
            "parameters": {
                "type": "object",
                "properties": {
                    "password": {
                        "type": "string",
                        "description": "The new password.",
                    },
                },
                "required": ["password"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scheduled_transactions",
            "description": "Get the list of scheduled transactions.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_scheduled_transaction",
            "description": "Update a scheduled transaction.",
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "The ID of the scheduled transaction.",
                    },
                    "recipient": {
                        "type": "string",
                        "description": "The new IBAN of the recipient.",
                    },
                    "amount": {
                        "type": "number",
                        "description": "The new amount.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "The new subject.",
                    },
                    "date": {
                        "type": "string",
                        "description": "The new date (YYYY-MM-DD).",
                    },
                    "recurring": {
                        "type": "boolean",
                        "description": "Whether the transaction is recurring.",
                    },
                },
                "required": ["id"],
            },
        },
    },
]

WORKSPACE_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email to the given recipients.",
            "parameters": {
                "type": "object",
                "properties": {
                    "recipients": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of recipient email addresses.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Subject of the email.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Body of the email.",
                    },
                },
                "required": ["recipients", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_email",
            "description": "Delete an email by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "The ID of the email to delete.",
                    },
                },
                "required": ["email_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read an email by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email_id": {
                        "type": "string",
                        "description": "The ID of the email to read.",
                    },
                },
                "required": ["email_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_sent_emails",
            "description": "Get all sent emails.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_received_emails",
            "description": "Get all received emails.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_draft_emails",
            "description": "Get all draft emails.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_emails",
            "description": "Search emails by query string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_unread_emails",
            "description": "Get all unread emails.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_contacts_by_name",
            "description": "Search contacts by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The name to search for.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_contacts_by_email",
            "description": "Search contacts by email address.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The email address to search for.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Create a new file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "The name of the file to create.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content of the file.",
                    },
                },
                "required": ["file_name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "The name of the file to read.",
                    },
                },
                "required": ["file_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "Delete a file by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "The name of the file to delete.",
                    },
                },
                "required": ["file_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_file",
            "description": "Append content to an existing file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_name": {
                        "type": "string",
                        "description": "The name of the file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The content to append.",
                    },
                },
                "required": ["file_name", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List all files in the workspace.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search files by query string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_day",
            "description": "Get the current day.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_events",
            "description": "Search calendar events by date or query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "date": {
                        "type": "string",
                        "description": "The date to search (YYYY-MM-DD).",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Create a new calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "The title of the event.",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "The start time (YYYY-MM-DD HH:MM).",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "The end time (YYYY-MM-DD HH:MM).",
                    },
                    "description": {
                        "type": "string",
                        "description": "The description of the event.",
                    },
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of participant email addresses.",
                    },
                },
                "required": ["title", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_day_calendar_events",
            "description": "Get all calendar events for a specific day.",
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {
                        "type": "string",
                        "description": "The date (YYYY-MM-DD).",
                    },
                },
                "required": ["day"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_calendar_event",
            "description": "Cancel a calendar event by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The ID of the event to cancel.",
                    },
                },
                "required": ["event_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_calendar_event",
            "description": "Reschedule a calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The ID of the event.",
                    },
                    "new_start_time": {
                        "type": "string",
                        "description": "New start time (YYYY-MM-DD HH:MM).",
                    },
                    "new_end_time": {
                        "type": "string",
                        "description": "New end time (YYYY-MM-DD HH:MM).",
                    },
                },
                "required": ["event_id", "new_start_time", "new_end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_calendar_event_participants",
            "description": "Add participants to a calendar event.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The ID of the event.",
                    },
                    "participants": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Email addresses to add.",
                    },
                },
                "required": ["event_id", "participants"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files_by_filename",
            "description": "Search files by filename.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "The filename to search for.",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_by_id",
            "description": "Get a file by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "The ID of the file.",
                    },
                },
                "required": ["file_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "share_file",
            "description": "Share a file with another user.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "The ID of the file to share.",
                    },
                    "email": {
                        "type": "string",
                        "description": "Email of the user to share with.",
                    },
                    "permission": {
                        "type": "string",
                        "description": "Permission level (r, rw, or w).",
                    },
                },
                "required": ["file_id", "email"],
            },
        },
    },
]

SUITE_TOOL_SCHEMAS = {
    "banking": BANKING_TOOL_SCHEMAS,
    "workspace": WORKSPACE_TOOL_SCHEMAS,
}
