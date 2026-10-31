import SwiftUI
import UserNotifications

@main
struct LimitPatternApp: App {
    @State private var state = AppState()
    @Environment(\.scenePhase) private var scenePhase

    init() { UNUserNotificationCenter.current().delegate = NotificationDelegate.shared }

    var body: some Scene {
        WindowGroup {
            Group {
                switch state.phase {
                case .checking: ProgressView("正在连接服务器…").frame(maxWidth: .infinity, maxHeight: .infinity).screenBackground()
                case .login: LoginView()
                case .ready: MainTabView()
                }
            }
            .environment(state)
            .tint(Theme.gold)
            .preferredColorScheme(.dark)
            .task { await state.bootstrap() }
            .onChange(of: state.phase) { _, p in
                if p == .ready { Notifier.requestPermission(); AlertPoller.shared.start() } else { AlertPoller.shared.stop() }
            }
            .onChange(of: scenePhase) { _, p in
                if p == .active, state.phase == .ready { AlertPoller.shared.start(); Task { await state.refreshHealth() } }
            }
        }
    }
}
