// Bug #2: the Branch-in-new-chat button used to render unconditionally even
// when its handler was a no-op (session-tile.tsx passed `() => undefined`
// for branched/tiled chats, where nested branching isn't supported). That
// left a visibly clickable button that silently did nothing. The fix makes
// AssistantMessage's action bar hide the button entirely when no handler is
// supplied, matching how onDismissError/onRestoreToMessage already behave.
import { AssistantRuntimeProvider, type ThreadMessage, useExternalStoreRuntime } from '@assistant-ui/react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { $displayTimestamps } from '@/store/display-timestamps'

import { stubThreadEnvironment } from '../test-utils'

import { formatClockTimestamp, formatTimelineRange } from './timestamp'

import { Thread } from '.'

// Timeline timestamps render only when `display.timestamps` is enabled.
$displayTimestamps.set(true)

const createdAt = new Date('2026-05-01T00:00:00.000Z')
const completedAt = createdAt.getTime() / 1000 + 1.25
stubThreadEnvironment()

afterEach(() => {
  cleanup()
})

function userMessage(): ThreadMessage {
  return {
    id: 'user-1',
    role: 'user',
    content: [{ type: 'text', text: 'question one' }],
    attachments: [],
    createdAt,
    metadata: { custom: { timelineTimestamp: createdAt.getTime() / 1000 } }
  } as unknown as ThreadMessage
}

function assistantMessage(): ThreadMessage {
  return {
    id: 'assistant-1',
    role: 'assistant',
    content: [
      {
        type: 'reasoning',
        text: 'checked carefully',
        timestamp: createdAt.getTime() / 1000 + 0.05,
        completedAt: createdAt.getTime() / 1000 + 0.1
      },
      {
        type: 'text',
        text: 'done',
        timestamp: createdAt.getTime() / 1000 + 0.125,
        completedAt: createdAt.getTime() / 1000 + 0.5
      }
    ],
    status: { type: 'complete', reason: 'stop' },
    createdAt,
    metadata: {
      unstable_state: null,
      unstable_annotations: [],
      unstable_data: [],
      steps: [],
      custom: { timelineCompletedAt: completedAt, timelineTimestamp: createdAt.getTime() / 1000 }
    }
  } as unknown as ThreadMessage
}

function Harness({
  assistant = assistantMessage(),
  onBranchInNewChat
}: {
  assistant?: ThreadMessage
  onBranchInNewChat?: (messageId: string) => void
}) {
  const runtime = useExternalStoreRuntime<ThreadMessage>({
    messages: [userMessage(), assistant],
    isRunning: false,
    onNew: async () => {}
  })

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <Thread onBranchInNewChat={onBranchInNewChat} />
    </AssistantRuntimeProvider>
  )
}

describe('AssistantMessage branch button visibility (bug #2 fix)', () => {
  it('shows the Branch in new chat button when a handler is provided (open chat)', async () => {
    render(<Harness onBranchInNewChat={() => undefined} />)

    expect(await screen.findByRole('button', { name: 'Branch in new chat' })).toBeTruthy()
  })

  it('hides the Branch in new chat button when no handler is provided (session-tile / branched chat)', async () => {
    render(<Harness />)

    // Wait for the assistant message to actually mount before asserting
    // absence, so a missing button isn't just a false negative from an
    // unrendered message.
    await screen.findByText('done')

    expect(screen.queryByRole('button', { name: 'Branch in new chat' })).toBeNull()
  })
})

describe('message timeline timestamps', () => {
  it('shows chat bubbles as a minute-precision clock and activity parts as precise ranges', async () => {
    const { container } = render(<Harness />)

    await screen.findByText('done')

    const stamps = Array.from(container.querySelectorAll('[data-slot="timeline-timestamp"]')).map(node =>
      node.textContent?.trim()
    )

    const startedAt = createdAt.getTime() / 1000

    // Bubble rows: "when was it sent" / "when did it land", no seconds.
    expect(stamps).toContain(formatClockTimestamp(startedAt))
    expect(stamps).toContain(formatClockTimestamp(completedAt))
    expect(stamps).not.toContain(formatTimelineRange(startedAt, completedAt))
    // The text part carries the prose, so it is a bubble row too — this is
    // the exact row that used to print `5:06:59.615 AM → 5:08:37.037 AM`.
    expect(stamps).not.toContain(formatTimelineRange(startedAt + 0.125, startedAt + 0.5))

    // Reasoning stays an activity boundary and keeps full precision.
    expect(stamps).toContain(formatTimelineRange(startedAt + 0.05, startedAt + 0.1))
  })

  it('renders the assistant landing clock from the completion time, not the send time', async () => {
    const startedAt = createdAt.getTime() / 1000
    // Deliberately crosses a minute boundary so "sent" and "landed" differ.
    const landedAt = startedAt + 130

    const assistant = {
      ...assistantMessage(),
      metadata: {
        ...assistantMessage().metadata,
        custom: { timelineCompletedAt: landedAt, timelineTimestamp: startedAt }
      }
    } as unknown as ThreadMessage

    const { container } = render(<Harness assistant={assistant} />)

    await screen.findByText('done')

    const stamps = Array.from(container.querySelectorAll('[data-slot="timeline-timestamp"]')).map(node =>
      node.textContent?.trim()
    )

    expect(formatClockTimestamp(landedAt)).not.toBe(formatClockTimestamp(startedAt))
    expect(stamps).toContain(formatClockTimestamp(landedAt))
  })

  it('suppresses an aggregate assistant stamp that exactly duplicates its sole part', async () => {
    const startedAt = createdAt.getTime() / 1000
    // Cross a minute boundary so the assistant's landing clock is textually
    // distinct from the user bubble's send clock and the two can be counted.
    const landedAt = startedAt + 130

    const assistant = {
      ...assistantMessage(),
      content: [{ completedAt: landedAt, text: 'done', timestamp: startedAt, type: 'text' }],
      metadata: {
        ...assistantMessage().metadata,
        custom: { timelineCompletedAt: landedAt, timelineTimestamp: startedAt }
      }
    } as unknown as ThreadMessage

    const { container } = render(<Harness assistant={assistant} />)

    await screen.findByText('done')

    const stamps = Array.from(container.querySelectorAll('[data-slot="timeline-timestamp"]')).map(node =>
      node.textContent?.trim()
    )

    // Aggregate and sole part now render identically, so exactly one survives
    // on the assistant bubble — plus the user bubble's own send clock.
    expect(formatClockTimestamp(landedAt)).not.toBe(formatClockTimestamp(startedAt))
    expect(stamps.filter(stamp => stamp === formatClockTimestamp(landedAt))).toHaveLength(1)
    expect(stamps.filter(stamp => stamp === formatClockTimestamp(startedAt))).toHaveLength(1)
    expect(stamps).not.toContain(formatTimelineRange(startedAt, landedAt))
  })
})
