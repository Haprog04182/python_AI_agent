# -*- coding: utf-8 -*-
"""Совместимость: переэкспорт адаптера Blender из blender_tool.py."""
from blender_tool import *  # noqa: F401,F403
from blender_tool import BlenderTool as _BlenderTool

BlenderMock = _BlenderTool
BlenderAdapter = _BlenderTool
BlenderClient = _BlenderTool
Blender = _BlenderTool
