import type { Meta, StoryObj } from "@storybook/react-vite"
import { AttachmentBubble } from "./AttachmentBubble"

const meta: Meta<typeof AttachmentBubble> = {
  title: "Molecules/AttachmentBubble",
  component: AttachmentBubble,
  parameters: { docs: { description: { component: "03-v1-scope.md \"Вложения\": uploading -> processing -> ready, or rejected/failed (ATT-002..008)." } } },
  decorators: [(Story) => <div className="max-w-sm p-6"><Story /></div>],
}
export default meta
type Story = StoryObj<typeof AttachmentBubble>

export const ImageReady: Story = {
  args: { attachment: { kind: "image", name: "hero.png", state: "ready", previewUrl: "https://images.unsplash.com/photo-1618005198919-d3d4b5a92ead?w=600&q=60" } },
}
export const FileUploading: Story = { args: { attachment: { kind: "file", name: "motion-specs-draft.pdf", state: "uploading" } } }
export const FileProcessing: Story = {
  name: "File — processing (virus scan, ATT stuck-closed)",
  args: { attachment: { kind: "file", name: "contract-v2.docx", state: "processing" } },
}
export const FileReady: Story = { args: { attachment: { kind: "file", name: "brand-guidelines.pdf", state: "ready", sizeLabel: "2.4 MB" } } }
export const FileRejected: Story = {
  args: { attachment: { kind: "file", name: "video.mov", state: "rejected", rejectionReason: "File type not supported" } },
}
export const FileFailed: Story = { args: { attachment: { kind: "file", name: "assets.zip", state: "failed" } } }
export const VoiceReady: Story = { args: { attachment: { kind: "voice", name: "Voice message", state: "ready", durationSeconds: 14 } } }
export const VoiceOwn: Story = {
  args: { attachment: { kind: "voice", name: "Voice message", state: "ready", durationSeconds: 42 }, own: true },
}
