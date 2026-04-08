"""In-process MCP server for project information.

Gives agents access to project configuration from the database.
"""
from claude_agent_sdk import tool, create_sdk_mcp_server

from openclow.services import project_service


@tool("get_project_info", "Get project details including tech stack and conventions", {
    "project_name": str,
})
async def get_project_info(args):
    project = await project_service.get_project_by_name(args["project_name"])
    if not project:
        return {"content": [{"type": "text", "text": f"Project '{args['project_name']}' not found"}]}

    info = (
        f"Project: {project.name}\n"
        f"Repo: {project.github_repo}\n"
        f"Branch: {project.default_branch}\n"
        f"Tech Stack: {project.tech_stack or 'Not specified'}\n"
        f"Description: {project.description or 'No description'}\n"
        f"Setup Commands: {project.setup_commands or 'None'}\n"
    )
    return {"content": [{"type": "text", "text": info}]}


@tool("get_coding_conventions", "Get coding conventions and patterns for a project", {
    "project_name": str,
})
async def get_coding_conventions(args):
    project = await project_service.get_project_by_name(args["project_name"])
    if not project:
        return {"content": [{"type": "text", "text": "Project not found"}]}

    conventions = project.agent_system_prompt or "No specific conventions defined. Follow existing code patterns."
    return {"content": [{"type": "text", "text": conventions}]}


# Create the in-process MCP server
project_info_server = create_sdk_mcp_server(
    name="project-info",
    tools=[get_project_info, get_coding_conventions],
)
