# viewer 运行时数据

本目录存放 mmd 查看器（apps/mermaid-viewer/viewer.html）的运行时评论数据，整目录不入 git（R11）。

| 文件 | 说明 | 生成方式 |
|------|------|----------|
| `reviews.db` | SQLite 评论库，按文件名为 key 存评论 JSON | 由 `viewer_server.py` 自动创建；查看器在前端打标后通过 `POST /api/reviews/<name>` 写入 |

删除本目录即可重置全部评论数据。