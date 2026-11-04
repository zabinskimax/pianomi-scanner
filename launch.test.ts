import { describe, expect, it } from 'vitest';
import { launchDecision } from './launch';

describe('starting the scanner from the dev server', () => {
  it('starts it only when it isn’t running, isn’t starting and is set up', () => {
    expect(launchDecision({ answers: false, installed: true, starting: false })).toBe('start');
    // Started by hand, or by an earlier request.
    expect(launchDecision({ answers: true, installed: true, starting: false })).toBe('running');
    expect(launchDecision({ answers: false, installed: true, starting: true })).toBe('starting');
    expect(launchDecision({ answers: false, installed: false, starting: false })).toBe('not-set-up');
  });
});
