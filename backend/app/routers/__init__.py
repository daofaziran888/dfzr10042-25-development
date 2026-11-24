"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import experiment_closure, resource

# 专用路由必须排在通用资源路由之前注册，否则 /api/{module} 会抢先匹配。
ROUTERS = [experiment_closure, resource]
