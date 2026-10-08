import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ModelFacts } from './OperationalFacts';
import type { ModelHealthResponse } from '../api/types';

describe('Gemini model facts', () => {
  const ready = {
    provider: 'antigravity_gemini', available: true, model_available: true,
    model_id: 'gemini-3.8-flash-high', actual_model_id: 'gemini-3.8-flash-high',
    model_identity_source: 'completion_probe', weight_digest: null,
    digest_status: 'REMOTE_WEIGHTS_NOT_EXPOSED',
  } as ModelHealthResponse;

  it('shows verified remote response without inventing a weight hash', () => {
    const { container } = render(<ModelFacts model={ready} />);
    expect(screen.getByText('Verified Gemini response')).toBeInTheDocument();
    expect(container).toHaveTextContent('gemini-3.8-flash-high');
    expect(container).not.toHaveTextContent('sha256:');
  });

  it('does not mark a relay downgrade as verified', () => {
    render(<ModelFacts model={{ ...ready, actual_model_id: 'gemini-3.5-flash-low' }} />);
    expect(screen.queryByText('Verified Gemini response')).not.toBeInTheDocument();
  });
});
