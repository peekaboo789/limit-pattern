import SwiftUI

struct MainTabView: View {
    @Environment(AppState.self) private var state
    @State private var tab = UserDefaults.standard.integer(forKey: "tab")

    var body: some View {
        TabView(selection: $tab) {
            Tab("总览", systemImage: "square.grid.2x2", value: 0) { NavigationStack { OverviewView(tab: $tab) } }
            Tab("榜单", systemImage: "list.number", value: 1) { NavigationStack { RankingView() } }
            Tab("监控", systemImage: "waveform.path.ecg", value: 2) { NavigationStack { MonitorView() } }
            Tab("自选", systemImage: "star", value: 3) { NavigationStack { WatchlistView() } }
            Tab("更多", systemImage: "ellipsis.circle", value: 4) { NavigationStack { MoreView() } }
        }
        .tabBarMinimizeBehavior(.never)
        .overlay(alignment: .top) {
            if let w = state.versionWarning { ErrorBanner(message: w).padding(.horizontal) }
        }
    }
}
