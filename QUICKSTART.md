# 🚀 Atlas 快速使用指南

## ✅ 部署状态

所有服务已成功部署并运行！

**本机IP**: `192.168.1.8`

---

## 🌐 访问地址

### 📱 从局域网任何设备访问

#### 电脑浏览器
```
API文档:  http://192.168.1.8:8000/api/docs
健康检查: http://192.168.1.8:8000/health
```

#### 手机/平板浏览器
```
API文档:  http://192.168.1.8:8000/api/docs
```

---

## 🎯 快速测试

### 1. 访问API文档
在浏览器打开: **http://192.168.1.8:8000/api/docs**

你会看到完整的Swagger UI界面，包含57个API端点。

### 2. 测试健康检查
```bash
curl http://192.168.1.8:8000/health
```

返回示例:
```json
{
  "success": true,
  "message": "服务运行正常",
  "data": {
    "status": "healthy",
    "version": "2.0.0",
    "service": "atlas-api"
  }
}
```

### 3. 登录系统
在Swagger UI中:
1. 找到 `POST /api/v1/auth/login` 端点
2. 点击 "Try it out"
3. 输入:
   ```json
   {
     "username": "admin",
     "password": "admin123"
   }
   ```
4. 点击 "Execute"
5. 复制返回的 `access_token`

### 4. 授权访问
1. 点击页面右上角的 **Authorize** 按钮（锁图标）
2. 输入: `Bearer YOUR_ACCESS_TOKEN`
3. 点击 "Authorize"
4. 现在可以测试所有需要认证的端点了！

---

## 📊 核心API端点

### 系统概览
```
GET /api/v1/system/overview
```
查看数据源、任务、文档的统计信息。

### 数据源管理
```
GET    /api/v1/sources              # 列表
POST   /api/v1/sources              # 创建
GET    /api/v1/sources/{name}       # 详情
PUT    /api/v1/sources/{name}       # 更新
DELETE /api/v1/sources/{name}       # 删除
```

### 文档管理
```
GET    /api/v1/documents/raw        # 原始文档列表
GET    /api/v1/documents/processed  # 处理后文档列表
```

### 监控仪表板
```
GET    /api/v1/monitoring/dashboard  # 监控数据
GET    /api/v1/monitoring/workflows  # 工作流列表
GET    /api/v1/monitoring/metrics     # 性能指标
```

---

## 🛠️ 管理命令

### 查看服务状态
```bash
./manage.sh status
```

### 查看日志
```bash
./manage.sh logs
```

### 重启服务
```bash
./manage.sh restart
```

---

## 📱 移动设备测试

### 从手机测试
1. 确保手机连接到同一Wi-Fi
2. 打开浏览器
3. 访问: `http://192.168.1.8:8000/api/docs`
4. 可以正常浏览和测试API

### 测试截图建议
- API文档界面
- 健康检查结果
- 登录成功
- 查看系统概览

---

## 🔧 故障排除

### 无法访问?
1. **检查服务状态**: `./manage.sh status`
2. **检查本机访问**: `curl http://localhost:8000/health`
3. **检查IP地址**: `hostname -I`（确认你的IP）

### CORS错误?
- 确认使用 `http://` 而不是 `https://`
- 尝试清除浏览器缓存

---

## 📚 更多信息

- **部署文档**: [DEPLOYMENT.md](./DEPLOYMENT.md)
- **项目总结**: [docs/project/PROJECT-SUMMARY.md](./docs/project/PROJECT-SUMMARY.md)
- **API文档**: http://192.168.1.8:8000/api/docs

---

## 🎉 享受使用Atlas！

系统已就绪，可以开始探索所有功能了！

**核心功能**:
- ✅ 数据采集和调度
- ✅ 多格式内容解析
- ✅ 智能去重
- ✅ 工作流编排
- ✅ 实时监控

**访问地址**: **http://192.168.1.8:8000/api/docs**

---
*生成时间: 2026-01-25*
