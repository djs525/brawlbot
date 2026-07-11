"""Plugin registry: discovers plugin files and routes tool calls to them.

A "plugin" is any module in this folder that defines:
  - TOOLS: a list of Gemini tool definitions
  - async def execute(name, tool_input) -> str
"""

import importlib
import pkgutil

_plugins = []      # list of loaded plugin modules
_tool_owner = {}   # maps tool name -> the module that owns it

def load_plugins():
    """Import every module in this folder and index its tools."""
    _plugins.clear()
    _tool_owner.clear()
    for info in pkgutil.iter_modules(__path__):
        module = importlib.import_module(f"{__name__}.{info.name}")

        if not hasattr(module, "TOOLS"):
            continue  # some helper file, not a plugin — ignore it

        _plugins.append(module)
        for tool in module.TOOLS:
            name = tool["name"]
            if name in _tool_owner:
                raise RuntimeError(
                    f"Two plugins both define a tool called '{name}'. "
                    f"Rename one of them."
                )
            _tool_owner[name] = module

        print(f"plugins: loaded '{info.name}' with {len(module.TOOLS)} tool(s)")

def all_tools():
    """Every tool from every plugin, combined in a menu for Gemini"""
    return [tool for module in _plugins for tool in module.TOOLS]

async def execute(name: str, tool_input: dict) -> str:
    """Route one tool call to whichever plugin owns that tool name."""
    module = _tool_owner.get(name)
    if module is None:
        return f"ERROR: no plugin provides a tool named '{name}'"
    try:
        return await module.execute(name, tool_input)
    except Exception as e:
        return f"ERROR while running '{name}': {type(e).__name__}: {e}"
