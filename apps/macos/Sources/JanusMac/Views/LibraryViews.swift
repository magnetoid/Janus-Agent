import SwiftUI

struct SkillsView: View {
    @Environment(AppStore.self) private var store
    @State private var filter = ""

    var body: some View {
        let shown = store.skills.filter {
            filter.isEmpty || $0.name.localizedCaseInsensitiveContains(filter)
                || $0.description.localizedCaseInsensitiveContains(filter)
        }
        VStack(alignment: .leading, spacing: 0) {
            PageHeader(title: "Skills", subtitle: "\(store.skills.count) installed. Janus loads a skill when a task needs it.") {
                TextField("Filter", text: $filter)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 200)
            }
            List(shown) { skill in
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Text(skill.name).font(.system(size: 13, weight: .medium))
                            if !skill.category.isEmpty {
                                Text(skill.category).font(.system(size: 11)).foregroundStyle(.tertiary)
                            }
                        }
                        Text(skill.description)
                            .font(.system(size: 12.5))
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                    }
                    Spacer()
                    Toggle("", isOn: Binding(
                        get: { skill.enabled },
                        set: { v in Task { await store.toggleSkill(skill.name, enabled: v) } }))
                        .toggleStyle(.switch)
                        .controlSize(.mini)
                        .labelsHidden()
                }
                .padding(.vertical, 4)
            }
            .listStyle(.inset)
            .scrollContentBackground(.hidden)
        }
        .task { await store.loadSkills() }
    }
}

struct CronView: View {
    @Environment(AppStore.self) private var store

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            PageHeader(title: "Scheduled", subtitle: "Recurring jobs Janus runs on its own. Ask Janus in chat to add one.") {
                Button { Task { await store.loadCron() } } label: { Image(systemName: "arrow.clockwise") }
                    .buttonStyle(.plain)
                    .foregroundStyle(.secondary)
            }
            if store.cronJobs.isEmpty {
                VStack(spacing: 10) {
                    Spacer()
                    Image(systemName: "clock").font(.system(size: 26)).foregroundStyle(.tertiary)
                    Text("No scheduled jobs yet.").foregroundStyle(.secondary)
                    Text("Try: “Every weekday at 8am, summarize my unread email.”")
                        .font(.system(size: 12)).foregroundStyle(.tertiary)
                    Spacer()
                }
                .frame(maxWidth: .infinity)
            } else {
                List(store.cronJobs) { job in
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(job.name.isEmpty ? job.id : job.name).font(.system(size: 13, weight: .medium))
                            Spacer()
                            Text(job.schedule).font(.system(size: 12).monospacedDigit()).foregroundStyle(.secondary)
                        }
                        Text(job.prompt).font(.system(size: 12.5)).foregroundStyle(.secondary).lineLimit(2)
                        HStack(spacing: 10) {
                            if !job.enabled { Text("Paused").foregroundStyle(.orange) }
                            if !job.nextRun.isEmpty { Text("Next: \(job.nextRun)") }
                        }
                        .font(.system(size: 11))
                        .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 4)
                }
                .listStyle(.inset)
                .scrollContentBackground(.hidden)
            }
        }
        .task { await store.loadCron() }
    }
}

struct PageHeader<Trailing: View>: View {
    let title: String
    let subtitle: String
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .bottom) {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 22, weight: .medium)).tracking(-0.3)
                Text(subtitle).font(.system(size: 12.5)).foregroundStyle(.secondary)
            }
            Spacer()
            trailing
        }
        .padding(.horizontal, 28)
        .padding(.top, 18)
        .padding(.bottom, 12)
    }
}
