import SwiftUI
import WebKit

struct MoreView: View {
    @Environment(AppState.self) private var state
    @AppStorage("local_alerts") private var localAlerts = true
    @State private var openExport = UserDefaults.standard.string(forKey: "page") == "export"
    @State private var openModels = UserDefaults.standard.string(forKey: "page") == "models"

    var body: some View {
        List {
            Section {
                NavigationLink { ExportCenterView() } label: { Label("数据导出（TXT / MD / JSON / CSV）", systemImage: "square.and.arrow.up.on.square") }
            }
            Section {
                NavigationLink { ModelListView() } label: { Label("AI 模型（一键切换）", systemImage: "cpu") }
            }
            Section("原生页面") {
                NavigationLink { CouncilView() } label: { Label("多模型讨论验证", systemImage: "person.3") }
                NavigationLink { SimilarityView() } label: { Label("个股相似度 / 前 100", systemImage: "magnifyingglass") }
                NavigationLink { NextDayPoolView() } label: { Label("次日观察池", systemImage: "binoculars") }
                NavigationLink { SummaryListView() } label: { Label("每日总结", systemImage: "doc.text") }
            }
            Section("网页版页面（同一登录）") {
                web("回测", "chart.line.uptrend.xyaxis", "/backtest")
                web("聚类分析", "circle.hexagongrid", "/cluster")
                web("涨停池", "flame", "/pool")
                web("设置（含企业微信）", "gearshape", "/settings")
                web("完整网页版", "safari", "/")
            }
            Section("手机提醒") {
                Toggle("App 打开时本地通知", isOn: $localAlerts)
                Text("App 在前台或刚切到后台时，会把榜单和自选股涨超 3% / 6% 的提醒发成本地通知，只在交易时段提醒；监控页涨超 6% 会标红框并震动。\n\niOS 关掉后台的 App 后无法继续轮询；真正的后台推送需要付费开发者账号（APNs）。想在锁屏也收到，请在服务器设置里接上企业微信推送。")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Section("服务器") {
                NavigationLink { ServerAccountsView() } label: { Label("服务器与账户", systemImage: "server.rack") }
                LabeledContent("地址", value: state.serverText)
                LabeledContent("服务器版本", value: state.serverVersion.isEmpty ? "—" : state.serverVersion)
                LabeledContent("App 需要", value: "≥ \(APIClient.minServerVersion)")
                LabeledContent("账号", value: state.authStatus["username"].string ?? "—")
                LabeledContent("App 版本", value: Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "")
                if let w = state.versionWarning { Text(w).font(.caption).foregroundStyle(.orange) }
                Button("退出登录", role: .destructive) { Task { await state.logout() } }
                Button("退出所有设备", role: .destructive) { Task { await state.logoutAll() } }
                Text("修改账号或密码在「设置」网页里，改完所有设备都要重新登录。").font(.caption).foregroundStyle(.secondary)
            }
            Section { Disclaimer().listRowBackground(Color.clear) }
        }
        .scrollContentBackground(.hidden)
        .screenBackground()
        .navigationTitle("更多")
        .navigationDestination(isPresented: $openExport) { ExportCenterView() }
        .navigationDestination(isPresented: $openModels) { ModelListView() }
    }

    func web(_ title: String, _ icon: String, _ path: String) -> some View {
        NavigationLink { WebPageView(title: title, path: path) } label: { Label(title, systemImage: icon) }
    }
}

// MARK: - 网页版回退（共享登录 cookie）
struct WebPageView: View {
    let title: String
    let path: String
    var body: some View {
        WebContainer(path: path)
            .ignoresSafeArea(edges: .bottom)
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
    }
}

struct WebContainer: UIViewRepresentable {
    let path: String
    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .default()
        let web = WKWebView(frame: .zero, configuration: cfg)
        web.isOpaque = false
        web.backgroundColor = UIColor(Theme.bg)
        web.navigationDelegate = context.coordinator
        web.uiDelegate = context.coordinator
        web.allowsBackForwardNavigationGestures = true
        let api = APIClient.shared
        guard let base = api.baseURL, let url = URL(string: base.absoluteString + path) else { return web }
        let cookies = api.storage.cookies(for: base) ?? []
        let store = cfg.websiteDataStore.httpCookieStore
        let group = DispatchGroup()
        for c in cookies { group.enter(); store.setCookie(c) { group.leave() } }
        group.notify(queue: .main) { web.load(URLRequest(url: url)) }
        return web
    }
    func updateUIView(_ uiView: WKWebView, context: Context) {}

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        /// 网页里的下载（导出）交给系统分享面板。
        func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse) async -> WKNavigationResponsePolicy {
            if let http = navigationResponse.response as? HTTPURLResponse,
               (http.value(forHTTPHeaderField: "Content-Disposition") ?? "").contains("attachment") || !navigationResponse.canShowMIMEType {
                return .download
            }
            return .allow
        }
        func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) { download.delegate = self }
        func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) { download.delegate = self }
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            if navigationAction.targetFrame == nil { webView.load(navigationAction.request) }
            return nil
        }
    }
}

extension WebContainer.Coordinator: WKDownloadDelegate {
    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String) async -> URL? {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(suggestedFilename)
        try? FileManager.default.removeItem(at: url)
        return url
    }
    func downloadDidFinish(_ download: WKDownload) {
        Task { @MainActor in
            guard let name = download.originalRequest?.url?.lastPathComponent else { return }
            _ = name
            let dir = FileManager.default.temporaryDirectory
            let newest = (try? FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: [.contentModificationDateKey]))?
                .filter { !$0.hasDirectoryPath }
                .max { (a, b) in
                    let da = (try? a.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                    let db = (try? b.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
                    return da < db
                }
            guard let file = newest,
                  let scene = UIApplication.shared.connectedScenes.first as? UIWindowScene,
                  let root = scene.keyWindow?.rootViewController else { return }
            var top = root; while let p = top.presentedViewController { top = p }
            let vc = UIActivityViewController(activityItems: [file], applicationActivities: nil)
            vc.popoverPresentationController?.sourceView = top.view
            top.present(vc, animated: true)
        }
    }
}
