import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Button, Card, Table, type Column } from '@/components/ui';
import './productWorkspace.css';
import { listProducts, productPath, type ProductSummary } from '@/services/products';

const columns: Column<ProductSummary>[] = [
  { key: 'name', header: 'Product', render: item => <Link className="font-semibold text-primary-600 hover:underline" to={productPath(item.id, item.project_ids[0])}>{item.name}</Link> },
  { key: 'team', header: 'Team', render: item => item.team?.name ?? 'Not assigned' },
  { key: 'project_ids', header: 'Delivery scopes', render: item => item.project_ids.length },
  { key: 'lifecycle_status', header: 'Status', render: item => <span className="capitalize">{item.lifecycle_status ?? 'Saved'}</span> },
];

export default function ProductsList() {
  const [items, setItems] = useState<ProductSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  async function load(next?: string) {
    setLoading(true);
    setError(false);
    try {
      const page = await listProducts(next);
      setItems((previous) => next ? [...previous, ...page.items] : page.items);
      setCursor(page.next_cursor);
    } catch {
      setError(true);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void load(); }, []);

  return (
    <main className="product-workspace space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div><p className="product-eyebrow">Your workspace</p><h1>Products</h1><p className="product-muted mt-2">Your people, tools and delivery context, together.</p></div>
        <Link className="product-primary" to="/products/new">Create product</Link>
      </header>
      <Card padding="none" className="overflow-hidden">
        {error && <div role="alert" className="m-6">Could not load products. <button className="underline" onClick={() => void load()}>Try again</button></div>}
        {!loading && !error && items.length === 0 && <p className="p-6 text-center text-gray-500">No products yet. Create your first product.</p>}
        {items.length > 0 && <Table columns={columns} data={items} keyExtractor={item => item.id} />}
        {loading && <p role="status" className="p-6 text-center text-gray-500">Loading products…</p>}
        {cursor && !loading && <div className="border-t border-gray-200 p-4 text-center"><Button variant="outline" onClick={() => void load(cursor)}>Load more</Button></div>}
      </Card>
    </main>
  );
}
