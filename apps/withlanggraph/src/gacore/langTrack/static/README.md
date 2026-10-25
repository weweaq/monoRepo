# Vendored front-end assets

| 文件 | 版本 | 用途 | 许可 |
| --- | --- | --- | --- |
| `cytoscape.min.js` | 3.30.2 | KG 全景图 / 时间演化的力导图渲染 | MIT（文件头保留 Cytoscape Consortium 版权） |

来源：`https://unpkg.com/cytoscape@3.30.2/dist/cytoscape.min.js`（由早期 `data/demos/vendor/` 原型复制固化）。

`kg_view.py` 在渲染时把该文件**内联**进 HTML，因此页面不依赖 CDN、可离线双击打开。升级时替换本文件并在 `tests/test_langTrack_kg_view.py` 跑一遍页面冒烟。
