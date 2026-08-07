# flyai-cli

飞猪酒店、机票查询命令行工具（TypeScript，streamable_http MCP）。

## 依赖

- Node.js >= 18
- 安装：`npm install`

## 构建配置（profile）

默认凭证与签名字符串在**构建时**写入 `src/build-profile.ts`，由 `scripts/generate-build-config.mjs` 根据 profile 生成。

1. 复制模板：`cp profile.example.json profile.json`
2. 编辑 `profile.json`，填写 `defaultAuthorization`、`signSecret` 等（`profile.json` 已加入 `.gitignore`，勿提交密钥）。
3. 运行时可用环境变量 **`FLYAI_SIGN_SECRET`** 覆盖构建期写入的 `signSecret`（非空则优先），便于分发 bundle 时不把密钥打进产物、改由部署环境注入；须与 MCP 侧配置的密钥一致。
4. 可选：通过环境变量指定 profile 路径（相对仓库根目录或绝对路径）：

   ```bash
   export FLYAI_PROFILE=/path/to/your-profile.json
   ```

未提供 `profile.json` 且未设置 `FLYAI_PROFILE` 时：**不覆盖**仓库里已有的 `src/build-profile.ts`，直接作为默认配置参与编译。仅当该文件也不存在时，才会用 `profile.example.json` 生成一份（例如全新克隆且未提交 `build-profile.ts` 时）。

## 构建方法

| 命令 | 说明 |
|------|------|
| `npm run generate:build-config` | 有 `FLYAI_PROFILE` 或 `profile.json` 时据此写入 `src/build-profile.ts`；否则若已有 `src/build-profile.ts` 则跳过 |
| `npm run build` | 先执行 `generate:build-config`（`prebuild`），再 `tsc` 编译到 `dist/`，并为 `dist/index.js` 加可执行权限 |
| `npm run build:bundle` | 先 `generate:build-config`，再用 esbuild 打单文件 `dist/flyai-bundle.cjs`（含 commander 等依赖） |
| `npm run build:bundle:obfuscate` | **一条指令**：先 `generate:build-config`，再 esbuild 打包并混淆；产出 **`dist/flyai-bundle.plain.cjs`**（未混淆中间产物）与 **`dist/flyai-bundle.cjs`**（混淆终产物，`package.json` 的 `flyai-bundle` 入口） |
| `npm run build:full` | `build` + `build:bundle:obfuscate`（完整编译 + 混淆单文件包） |

说明：`npm run build` 与 `npm run build:bundle` 会通过 `prebuild` / `prebuild:bundle` 自动跑 profile 生成；**混淆单文件包**请直接用 `npm run build:bundle:obfuscate`（已内含 `generate:build-config`，无需再单独执行）。供安全审计时优先使用 **`npm run build`** 或 **`npm run build:bundle`（无 obfuscate）**；混淆构建请保留并对照 **`dist/flyai-bundle.plain.cjs`**。

## 运行

- 开发（含生成配置、编译、启动）：`npm run dev`
- 编译产物：`node dist/index.js <command> ...`
- 单文件包：`node dist/flyai-bundle.cjs`（需先执行对应 bundle 构建）；若使用混淆构建，还可直接运行未混淆的 `node dist/flyai-bundle.plain.cjs` 便于排查问题
- 全局命令：在仓库根目录执行 `npm link` 后使用 `flyai <command> ...`

## 隐私与请求头 x-ff-ctx

每个 MCP 请求会携带 **`x-ff-ctx`**：内容为 gzip 后的 JSON，若已配置 `signSecret`（或运行时 **`FLYAI_SIGN_SECRET`**）则再经 AES-256-GCM 加密后 Base64。典型用途包括风控、滥用检测与客户端环境一致性校验。CLI **默认不采集主机名、系统用户名、主目录**；CPU 仅传核数，不传型号。具体字段实现见 `src/machine-info.ts`、`src/device-id.ts`。

**在变更默认设备标识之前，请与飞猪 MCP / 风控侧确认**，避免影响鉴权、签名或风控策略。

### 载荷概要

- **machine**：`platform`、`arch`、CPU 核数、内存档位（粗分档 GB）、`osType`、`nodeVersion`、`osReleaseMajor`（内核/系统主版本号）
- **fingerprint**：主语言标签（不含完整 locale）、UA 风格 `platform`、`User-Agent` 字符串、硬件并发数、粗设备内存档、`clientSurface: cli`、时区偏移、`deviceId`（对随机种子做 SHA256 的 64 位 hex）。种子来自 `~/.flyai/device-id` 或临时目录文件；**不**读取 Linux `machine-id`、macOS `IOPlatformUUID`、Windows `MachineGuid`。

### 环境变量（隐私相关）

| 变量 | 取值 | 说明 |
|------|------|------|
| `FLYAI_SIGN_SECRET` | 非空字符串 | 覆盖构建期 `signSecret`，用于 `x-ff-ctx` 加密与请求 HMAC；须与服务端一致 |

## License

MIT
