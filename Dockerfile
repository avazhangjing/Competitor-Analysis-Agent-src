FROM python:3.11-slim

WORKDIR /app

# 后端依赖
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

# 后端源码
COPY app /app/app
# 前端构建产物
COPY frontend/dist /app/frontend/dist
# 版本说明（/api/changelog 接口读取）
COPY CHANGELOG.md /app/CHANGELOG.md

WORKDIR /app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]