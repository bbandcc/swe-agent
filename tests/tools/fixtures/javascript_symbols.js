// Unicode byte-offset prefix: 雪 🧭
export async function outer(value) {
  function nested() {
    return value;
  }
  const arrow = async (item) => {
    return item;
  };
  class Inner {
    method(
      item,
    ) {
      return item;
    }
  }
  return arrow(value);
}

export class Worker {
  async run(value) {
    return value;
  }
}

export const exportedArrow = (value) => value;
