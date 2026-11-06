import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Select } from '@/components/ui';
import { listProducts, type ProductSummary } from '@/services/products';

interface Props {
  flowId: string;
  productId: string | null;
  projectId?: string | null;
  disabled: boolean;
  onChange: (dirty: boolean) => void;
  onSave: (productId: string, projectId?: string) => void;
}

export function DraftProductSelector({ flowId, productId, projectId, disabled, onChange, onSave }: Props) {
  const [selected, setSelected] = useState(productId ?? '');
  const [selectedProject, setSelectedProject] = useState(projectId ?? '');
  const products = useQuery({
    queryKey: ['products', 'draft-binding'],
    queryFn: async () => {
      const items: ProductSummary[] = [];
      let cursor: string | undefined;
      do {
        const page = await listProducts(cursor);
        items.push(...page.items);
        cursor = page.next_cursor ?? undefined;
      } while (cursor);
      return items;
    },
    retry: false,
  });
  const available = products.data ?? [];
  const missingSelection = selected && !available.some(product => product.id === selected);
  const product = available.find(item => item.id === selected);
  const effectiveProject = selectedProject || product?.base_project_id || '';
  const dirty = selected !== (productId ?? '') || effectiveProject !== (projectId || product?.base_project_id || '');
  return <div className="space-y-2 rounded border p-3">
    <Select id={`draft-product-${flowId}`} label="Product" required value={selected}
      disabled={disabled || products.isFetching || products.isError}
      options={[
        { value: '', label: 'Select a Product' },
        ...(missingSelection ? [{ value: selected, label: 'Current Product unavailable', disabled: true }] : []),
        ...available.map(product => ({ value: product.id, label: product.name })),
      ]}
      onChange={event => {
        setSelected(event.target.value);
        setSelectedProject('');
        onChange(event.target.value !== (productId ?? ''));
      }} />
    {product && <Select id={`draft-project-${flowId}`} label="Project" value={selectedProject} disabled={disabled || products.isFetching}
      options={[{ value: '', label: 'Base Project (default)' }, ...(product.projects ?? []).map(item => ({ value: item.id, label: item.name + (item.is_base ? ' (Base)' : '') }))]}
      onChange={event => { setSelectedProject(event.target.value); onChange(selected !== (productId ?? '') || (event.target.value || product.base_project_id || '') !== (projectId || product.base_project_id || '')); }} />}
    {products.isPending && <p>Loading Products…</p>}
    {products.isError && <Alert variant="error" title="Products unavailable">Reload Products to try again.</Alert>}
    {products.isSuccess && !available.length && <p>Create a Product, then reload this list.</p>}
    <p>Save the Product, then review and approve the updated plan. Saving does not start work.</p>
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" disabled={disabled || products.isFetching || products.isError || !selected || Boolean(missingSelection) || !dirty}
        onClick={() => onSave(selected, effectiveProject || undefined)}>Save Product</Button>
      <Button size="sm" variant="secondary" disabled={disabled || products.isFetching}
        onClick={() => { void products.refetch(); }}>Reload Products</Button>
      <a href="/products/new" target="_blank" rel="noreferrer" className="text-primary-600 underline">Create Product (new tab)</a>
    </div>
  </div>;
}
