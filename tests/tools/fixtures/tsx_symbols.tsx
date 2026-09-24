// Unicode byte-offset prefix: 雪 🧭
type Props<T extends string> = { title: T; onClick?: () => void };
export async function load<T extends { id: string }>(props: Props<T>): Promise<JSX.Element> {
  return <Widget title={props.title} onClick={(event: MouseEvent) => event.preventDefault()} />;
}
export class Panel<T extends string> extends Base<T> {
  render(value: T): JSX.Element {
    return <><Widget value={value} /><span>{value}</span></>;
  }
}
export const Generic = <T extends string,>(props: Props<T>) => (
  <>
    <Widget title={props.title} onClick={() => props.onClick?.()} />
    <Icon name="generic" />
  </>
);
export const SelfClosing = () => <Icon label={"雪"} />;
