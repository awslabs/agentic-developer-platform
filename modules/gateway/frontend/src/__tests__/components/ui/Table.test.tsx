import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Table, type Column } from '@/components/ui/Table';

interface TestItem {
  id: string;
  name: string;
  value: number;
}

describe('Table', () => {
  const columns: Column<TestItem>[] = [
    { key: 'name', header: 'Name' },
    { key: 'value', header: 'Value', align: 'right' },
  ];

  const data: TestItem[] = [
    { id: '1', name: 'Item 1', value: 100 },
    { id: '2', name: 'Item 2', value: 200 },
    { id: '3', name: 'Item 3', value: 300 },
  ];

  it('renders table with headers', () => {
    render(<Table columns={columns} data={data} keyExtractor={(item) => item.id} />);

    expect(screen.getByText('Name')).toBeInTheDocument();
    expect(screen.getByText('Value')).toBeInTheDocument();
  });

  it('renders table data', () => {
    render(<Table columns={columns} data={data} keyExtractor={(item) => item.id} />);

    expect(screen.getByText('Item 1')).toBeInTheDocument();
    expect(screen.getByText('Item 2')).toBeInTheDocument();
    expect(screen.getByText('Item 3')).toBeInTheDocument();
    expect(screen.getByText('100')).toBeInTheDocument();
    expect(screen.getByText('200')).toBeInTheDocument();
    expect(screen.getByText('300')).toBeInTheDocument();
  });

  it('renders empty message when no data', () => {
    render(
      <Table
        columns={columns}
        data={[]}
        keyExtractor={(item) => item.id}
        emptyMessage="No items found"
      />
    );

    expect(screen.getByText('No items found')).toBeInTheDocument();
  });

  it('handles row click', () => {
    const handleRowClick = vi.fn();
    render(
      <Table
        columns={columns}
        data={data}
        keyExtractor={(item) => item.id}
        onRowClick={handleRowClick}
      />
    );

    fireEvent.click(screen.getByText('Item 1'));
    expect(handleRowClick).toHaveBeenCalledWith(data[0]);
  });

  it('renders custom cell content with render prop', () => {
    const customColumns: Column<TestItem>[] = [
      {
        key: 'name',
        header: 'Name',
        render: (item) => <strong data-testid="custom">{item.name}</strong>,
      },
    ];

    render(<Table columns={customColumns} data={data} keyExtractor={(item) => item.id} />);

    const customElements = screen.getAllByTestId('custom');
    expect(customElements).toHaveLength(3);
    expect(customElements[0]).toHaveTextContent('Item 1');
  });

  it('renders loading state', () => {
    render(
      <Table columns={columns} data={[]} keyExtractor={(item) => item.id} isLoading={true} />
    );

    // Check for loading spinner
    expect(document.querySelector('.animate-spin')).toBeInTheDocument();
  });

  it('handles sortable columns', () => {
    const handleSort = vi.fn();
    const sortableColumns: Column<TestItem>[] = [
      { key: 'name', header: 'Name', sortable: true },
      { key: 'value', header: 'Value', sortable: true },
    ];

    render(
      <Table
        columns={sortableColumns}
        data={data}
        keyExtractor={(item) => item.id}
        onSort={handleSort}
        sortBy="name"
        sortOrder="asc"
      />
    );

    // Click on sortable column header
    fireEvent.click(screen.getByText('Name'));
    expect(handleSort).toHaveBeenCalledWith('name');
  });
});
