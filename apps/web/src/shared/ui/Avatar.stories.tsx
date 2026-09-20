import type { Meta, StoryObj } from "@storybook/react-vite"
import { Avatar } from "./Avatar"

const meta: Meta<typeof Avatar> = {
  title: "Shared UI/Avatar",
  component: Avatar,
  args: { name: "Anna Petrova" },
}
export default meta
type Story = StoryObj<typeof Avatar>

export const WithPhoto: Story = {
  args: {
    src: "https://lh3.googleusercontent.com/aida-public/AB6AXuCc-RlBVkGKNtHrJPjcLNZYrvLDbVi2BLJodsCPX6Tg-AYMPAbmQZry3FYxK-BgUuMEhzqFKGw4NtfHctyZU_KZjd0_a3sn-53RucMw_zP-msw0K9qCb7zL9Leg4G12477hs3y8MJgzpSN0KB86USeqgu0AP9Qb5i3x21WmLTgdHL17DMd8OhY1p0tXkrecwqG-kkM9OLbFHWezsXMYY5Y4WiS-yN4lqCCkZMXc7nZYFbjzrkRgVVT4",
    presence: "online",
  },
}
export const InitialsFallback: Story = { args: { name: "Elena Rostova", initials: "ER" } }
export const AwayPresence: Story = { args: { name: "Marcus Chen", presence: "away" } }
export const NoPresence: Story = { args: { name: "Sofia Rossi" } }

export const Sizes: Story = {
  render: () => (
    <div className="flex items-end gap-4 p-4">
      <Avatar name="Anna Petrova" size="sm" presence="online" />
      <Avatar name="Anna Petrova" size="md" presence="online" />
      <Avatar name="Anna Petrova" size="lg" presence="online" />
    </div>
  ),
}
